//! SlimeVault's world-key policy and retained revision access.
use hyper::{Request, Response};
use serde::Serialize;
use std::convert::TryInto;

use garage_api_common::helpers::*;
use garage_model::s3::object_table::*;
use garage_table::EnumerationOrder;

use crate::api_server::ResBody;
use crate::error::*;
use crate::router::Endpoint;

pub fn validate_key(key: &str) -> Result<(), Error> {
	if key.len() > 1024
		|| !key.ends_with(".slime")
		|| key
			.split('/')
			.any(|s| s.is_empty() || s == "." || s == "..")
		|| key.chars().any(|c| c.is_control() || c == '\\')
		|| key.rsplit('/').next() == Some(".slime")
	{
		return Err(Error::bad_request("SlimeVault accepts only nonempty .slime world keys without empty, dot or parent path segments"));
	}
	Ok(())
}

pub fn validate_endpoint(endpoint: &Endpoint) -> Result<(), Error> {
	match endpoint {
		Endpoint::PutObject { key }
		| Endpoint::CopyObject { key }
		| Endpoint::CreateMultipartUpload { key }
		| Endpoint::UploadPart { key, .. }
		| Endpoint::UploadPartCopy { key, .. }
		| Endpoint::CompleteMultipartUpload { key, .. }
		| Endpoint::AbortMultipartUpload { key, .. }
		| Endpoint::DeleteObject { key, .. } => validate_key(key),
		// Batch bodies must not bypass key/version policy. Single DELETE is supported.
		Endpoint::DeleteObjects {} => Err(Error::bad_request(
			"Use single-object DELETE; bulk deletion is disabled in SlimeVault mode",
		)),
		Endpoint::PutBucketLifecycleConfiguration {} => Err(Error::bad_request(
			"Automatic expiration is disabled in SlimeVault mode to preserve world history",
		)),
		_ => Ok(()),
	}
}

pub fn request_version<B>(req: &Request<B>) -> Result<Option<String>, Error> {
	let values: Vec<_> = url::form_urlencoded::parse(req.uri().query().unwrap_or("").as_bytes())
		.filter(|(k, _)| k == "versionId")
		.map(|(_, v)| v.into_owned())
		.collect();
	if values.len() > 1 {
		return Err(Error::bad_request("Duplicate versionId"));
	}
	Ok(values.into_iter().next())
}

pub fn select_version<'a>(
	object: &'a Object,
	id: Option<&str>,
) -> Result<&'a ObjectVersion, Error> {
	if let Some(id) = id {
		if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
			return Err(Error::bad_request(
				"versionId must be a 64-character hexadecimal revision ID",
			));
		}
		object
			.versions()
			.iter()
			.find(|v| v.is_complete() && hex::encode(v.uuid).eq_ignore_ascii_case(id))
			.ok_or(Error::NoSuchVersion)
	} else {
		object
			.versions()
			.iter()
			.rev()
			.find(|v| v.is_complete())
			.ok_or(Error::NoSuchKey)
	}
}

#[derive(Serialize)]
#[serde(rename = "ListVersionsResult", rename_all = "PascalCase")]
struct VersionsResult {
	#[serde(rename = "@xmlns")]
	xmlns: &'static str,
	name: String,
	prefix: String,
	key_marker: String,
	version_id_marker: String,
	#[serde(skip_serializing_if = "Option::is_none")]
	next_key_marker: Option<String>,
	#[serde(skip_serializing_if = "Option::is_none")]
	next_version_id_marker: Option<String>,
	max_keys: u64,
	#[serde(skip_serializing_if = "Option::is_none")]
	encoding_type: Option<String>,
	is_truncated: bool,
	#[serde(rename = "$value")]
	entries: Vec<VersionEntry>,
}
#[derive(Serialize)]
enum VersionEntry {
	Version(Revision),
	DeleteMarker(Revision),
}
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Revision {
	key: String,
	version_id: String,
	is_latest: bool,
	last_modified: String,
	#[serde(rename = "ETag", skip_serializing_if = "Option::is_none")]
	etag: Option<String>,
	#[serde(skip_serializing_if = "Option::is_none")]
	size: Option<u64>,
	#[serde(skip_serializing_if = "Option::is_none")]
	storage_class: Option<&'static str>,
}

/// Iterate by object key, then newest-first revision, using S3 key/version markers.
/// Delimiter grouping and URL encoding are rejected explicitly rather than ignored.
pub async fn list_versions(
	ctx: ReqCtx,
	prefix: Option<String>,
	key_marker: Option<String>,
	version_id_marker: Option<String>,
	max_keys: Option<u64>,
	delimiter: Option<String>,
	encoding_type: Option<String>,
) -> Result<Response<ResBody>, Error> {
	if delimiter.is_some() || encoding_type.as_deref().is_some_and(|s| s != "url") {
		return Err(Error::bad_request(
			"ListObjectVersions currently supports flat keys with optional encoding-type=url and without delimiter",
		));
	}
	if version_id_marker.is_some() && key_marker.is_none() {
		return Err(Error::bad_request("version-id-marker requires key-marker"));
	}
	let prefix = prefix.unwrap_or_default();
	let max_keys = max_keys.unwrap_or(1000).min(1000);
	let mut result = VersionsResult {
		xmlns: "http://s3.amazonaws.com/doc/2006-03-01/",
		name: ctx.bucket_name,
		prefix: prefix.clone(),
		key_marker: key_marker.clone().unwrap_or_default(),
		version_id_marker: version_id_marker.clone().unwrap_or_default(),
		next_key_marker: None,
		next_version_id_marker: None,
		max_keys,
		encoding_type: encoding_type.clone(),
		is_truncated: false,
		entries: vec![],
	};
	if max_keys == 0 {
		return xml_response(&mut result);
	}
	let start = key_marker
		.clone()
		.unwrap_or_else(|| prefix.clone())
		.max(prefix.clone());
	let mut cursor = start;
	let mut skip_cursor = false;
	let mut last_emitted = None;
	'scan: loop {
		let objects = ctx
			.garage
			.object_table
			.get_range(
				&ctx.bucket_id,
				Some(cursor.clone()),
				None,
				128,
				EnumerationOrder::Forward,
			)
			.await?;
		if objects.is_empty() {
			break;
		}
		let count = objects.len();
		let next = objects.last().unwrap().key.clone();
		for object in objects {
			if skip_cursor && object.key == cursor {
				continue;
			}
			if !object.key.starts_with(&prefix) {
				break 'scan;
			}
			let mut past_marker = key_marker.as_deref() != Some(&object.key);
			if !past_marker && version_id_marker.is_none() {
				continue;
			}
			let mut latest = true;
			for v in object.versions().iter().rev().filter(|v| v.is_complete()) {
				let id = hex::encode(v.uuid);
				let is_latest = latest;
				latest = false;
				if !past_marker {
					if version_id_marker.as_deref() == Some(&id) {
						past_marker = true;
					}
					continue;
				}
				if result.entries.len() == max_keys as usize {
					result.is_truncated = true;
					if let Some((key, id)) = last_emitted {
						result.next_key_marker = Some(key);
						result.next_version_id_marker = Some(id);
					}
					break 'scan;
				}
				let (meta, deleted) = match &v.state {
					ObjectVersionState::Complete(ObjectVersionData::Inline(m, _))
					| ObjectVersionState::Complete(ObjectVersionData::FirstBlock(m, _)) => (Some(m), false),
					_ => (None, true),
				};
				let revision = Revision {
					key: object.key.clone(),
					version_id: id.clone(),
					is_latest,
					last_modified: chrono::DateTime::from_timestamp_millis(v.timestamp as i64)
						.ok_or_internal_error("Invalid revision timestamp")?
						.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
					etag: meta.map(|m| format!("\"{}\"", m.etag)),
					size: meta.map(|m| m.size),
					storage_class: if deleted { None } else { Some("STANDARD") },
				};
				result.entries.push(if deleted {
					VersionEntry::DeleteMarker(revision)
				} else {
					VersionEntry::Version(revision)
				});
				last_emitted = Some((object.key.clone(), id));
			}
		}
		if count < 128 || next == cursor {
			break;
		}
		cursor = next;
		skip_cursor = true;
	}
	xml_response(&mut result)
}
fn xml_response(result: &mut VersionsResult) -> Result<Response<ResBody>, Error> {
	if result.encoding_type.as_deref() == Some("url") {
		let encode = |s: &str| garage_api_common::encoding::uri_encode(s, true);
		result.prefix = encode(&result.prefix);
		result.key_marker = encode(&result.key_marker);
		result.next_key_marker = result.next_key_marker.as_deref().map(encode);
		for entry in &mut result.entries {
			let (VersionEntry::Version(v) | VersionEntry::DeleteMarker(v)) = entry;
			v.key = encode(&v.key);
		}
	}
	Ok(Response::builder()
		.header("Content-Type", "application/xml")
		.body(string_body(crate::xml::to_xml_with_header(result)?))?)
}

#[cfg(test)]
mod tests {
	use super::*;
	#[test]
	fn world_keys() {
		for key in [
			"island.slime",
			"worlds/player-1.slime",
			"templates/arena.slime",
			"instances/match.slime",
		] {
			assert!(validate_key(key).is_ok());
		}
		for key in [
			"",
			".slime",
			"worlds/.slime",
			"x.txt",
			"../x.slime",
			"x//y.slime",
			"/x.slime",
			"x\\y.slime",
			"x\ny.slime",
		] {
			assert!(validate_key(key).is_err(), "{key}");
		}
	}
}

pub async fn put_versioning(
	ctx: ReqCtx,
	req: Request<crate::api_server::ReqBody>,
) -> Result<Response<ResBody>, Error> {
	let body = req.into_body().collect().await?;
	let xml = roxmltree::Document::parse(std::str::from_utf8(&body)?)?;
	let root = xml.root_element();
	let status = root
		.children()
		.find(|n| n.has_tag_name("Status"))
		.and_then(|n| n.text());
	if !ctx.garage.config.s3_api.slime_world_only
		|| !root.has_tag_name("VersioningConfiguration")
		|| status != Some("Enabled")
	{
		return Err(Error::bad_request(
			"SlimeVault world versioning is always enabled and cannot be suspended",
		));
	}
	Ok(Response::builder().body(empty_body())?)
}

pub async fn insert_world(ctx: &ReqCtx, object: &Object) -> Result<(), Error> {
	if !object.key.ends_with(".slime") {
		ctx.garage.object_table.insert(object).await?;
		return Ok(());
	}
	let reply = ctx
		.garage
		.world_control
		.commit(
			object.clone(),
			garage_model::s3::control::WriteAuth {
				actor: ctx.api_key.key_id.clone(),
				token: ctx.world_lease_token.clone(),
				expected_revision: ctx.world_expected_revision.clone(),
			},
		)
		.await?;
	check_control(reply)?;
	Ok(())
}
fn check_control(
	reply: garage_model::s3::control::Reply,
) -> Result<garage_model::s3::control::Reply, Error> {
	if let garage_model::s3::control::Reply::Denied { code, message } = reply {
		return Err(Error::WorldControl { code, message });
	}
	Ok(reply)
}

pub async fn control_request(
	ctx: ReqCtx,
	req: Request<crate::api_server::ReqBody>,
	endpoint: &Endpoint,
) -> Result<Response<ResBody>, Error> {
	use garage_model::s3::control::{Operation, Retention};
	let key = match endpoint {
		Endpoint::PutObject { key } | Endpoint::GetObject { key, .. } => key.clone(),
		_ => {
			return Err(Error::bad_request(
				"World control operations use PUT or GET on a world key",
			))
		}
	};
	validate_key(&key)?;
	let name = req
		.headers()
		.get("x-slimevault-operation")
		.unwrap()
		.to_str()?
		.to_owned();
	let owner_operation = matches!(
		name.as_str(),
		"protect" | "unprotect" | "retention" | "collect" | "purge"
	);
	if owner_operation && !ctx.api_key.allow_owner(&ctx.bucket_id) {
		return Err(Error::forbidden(
			"World protection and retention require bucket owner permission",
		));
	}
	if name != "inspect" && !matches!(endpoint, Endpoint::PutObject { .. }) {
		return Err(Error::bad_request("Mutating world operations require PUT"));
	}
	let (mut stream, checksums) = req.into_body().streaming_with_checksums();
	let mut body = Vec::new();
	while let Some(chunk) = futures::TryStreamExt::try_next(&mut stream).await? {
		if body.len().saturating_add(chunk.len()) > 16_384 {
			return Err(Error::bad_request("World control body exceeds 16 KiB"));
		}
		body.extend_from_slice(&chunk);
	}
	drop(stream);
	checksums
		.await
		.ok_or_internal_error("Control request checksum task")??;
	if body.len() > 16_384 {
		return Err(Error::bad_request("World control body exceeds 16 KiB"));
	}
	let args: serde_json::Value = if body.is_empty() {
		serde_json::json!({})
	} else {
		serde_json::from_slice(&body).ok_or_bad_request("Invalid control JSON")?
	};
	let string = |name: &str| -> Result<String, Error> {
		args.get(name)
			.and_then(|v| v.as_str())
			.map(str::to_owned)
			.ok_or_else(|| Error::bad_request(format!("Missing {name}")))
	};
	for field in ["ttl_ms", "keep_last", "max_age_ms"] {
		if args.get(field).is_some_and(|v| !v.is_u64()) {
			return Err(Error::bad_request(format!(
				"{field} must be an unsigned integer"
			)));
		}
	}
	if args.get("dry_run").is_some_and(|v| !v.is_boolean()) {
		return Err(Error::bad_request("dry_run must be boolean"));
	}
	let ttl_ms = args
		.get("ttl_ms")
		.and_then(|v| v.as_u64())
		.unwrap_or(30_000);
	let operation = match name.as_str() {
		"lease-acquire" => Operation::Acquire { ttl_ms },
		"lease-renew" => Operation::Renew {
			token: string("token")?,
			ttl_ms,
		},
		"lease-release" => Operation::Release {
			token: string("token")?,
		},
		"inspect" => Operation::Inspect,
		"protect" => Operation::Protect {
			revision: string("revision")?,
		},
		"unprotect" => Operation::Unprotect,
		"retention" => Operation::SetRetention(Retention {
			keep_last: args
				.get("keep_last")
				.and_then(|v| v.as_u64())
				.unwrap_or(10)
				.try_into()
				.ok_or_bad_request("keep_last overflow")?,
			max_age_ms: args.get("max_age_ms").and_then(|v| v.as_u64()),
		}),
		"collect" => Operation::Collect {
			dry_run: args
				.get("dry_run")
				.and_then(|v| v.as_bool())
				.unwrap_or(true),
		},
		"purge" => Operation::Purge {
			revision: string("revision")?,
		},
		_ => return Err(Error::bad_request("Unknown SlimeVault operation")),
	};
	let reply = check_control(
		ctx.garage
			.world_control
			.operate(ctx.bucket_id, key, ctx.api_key.key_id, operation)
			.await?,
	)?;
	Ok(Response::builder()
		.header("Content-Type", "application/json")
		.body(string_body(
			serde_json::to_string(&reply).ok_or_internal_error("Control response serialization")?,
		))?)
}

pub fn mutation_key(endpoint: &Endpoint) -> Option<&str> {
	match endpoint {
		Endpoint::PutObject { key }
		| Endpoint::CopyObject { key }
		| Endpoint::CreateMultipartUpload { key }
		| Endpoint::UploadPart { key, .. }
		| Endpoint::UploadPartCopy { key, .. }
		| Endpoint::CompleteMultipartUpload { key, .. }
		| Endpoint::AbortMultipartUpload { key, .. }
		| Endpoint::DeleteObject { key, .. } => Some(key),
		_ => None,
	}
}
pub async fn check_world_write(ctx: &ReqCtx, key: &str) -> Result<(), Error> {
	if key.ends_with(".slime") {
		check_control(
			ctx.garage
				.world_control
				.check_write(
					ctx.bucket_id,
					key.to_owned(),
					garage_model::s3::control::WriteAuth {
						actor: ctx.api_key.key_id.clone(),
						token: ctx.world_lease_token.clone(),
						expected_revision: ctx.world_expected_revision.clone(),
					},
				)
				.await?,
		)?;
	}
	Ok(())
}
