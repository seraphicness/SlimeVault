//! Fixed-authority world coordination. No local fallback or automatic failover.
//! Requests are serialized at one durable coordinator, including final table publication.
use super::object_table::*;
use garage_db as db;
use garage_rpc::{system::System, *};
use garage_table::{replication::TableShardedReplication, Table};
use garage_util::{
	background::{BackgroundRunner, Worker, WorkerState},
	config::Config,
	data::*,
	error::*,
	time::now_msec,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::convert::TryFrom;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{watch, Mutex};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct WriteAuth {
	pub actor: String,
	pub token: Option<String>,
	pub expected_revision: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lease {
	pub token: String,
	pub actor: String,
	pub fence: u64,
	pub expires_at: u64,
	epoch: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Retention {
	pub keep_last: usize,
	pub max_age_ms: Option<u64>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorldState {
	pub bucket: Uuid,
	pub key: String,
	pub generation: u64,
	pub lease: Option<Lease>,
	pub frozen: bool,
	pub protected: BTreeSet<String>,
	pub retention: Option<Retention>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Operation {
	Acquire { ttl_ms: u64 },
	Renew { token: String, ttl_ms: u64 },
	Release { token: String },
	Inspect,
	Protect { revision: String },
	Unprotect,
	SetRetention(Retention),
	Collect { dry_run: bool },
	Purge { revision: String },
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Reply {
	State(WorldState),
	Collected {
		revisions: Vec<String>,
		dry_run: bool,
	},
	Ok,
	Denied {
		code: String,
		message: String,
	},
}
#[derive(Debug, Serialize, Deserialize)]
enum Message {
	CheckWrite {
		bucket: Uuid,
		key: String,
		auth: WriteAuth,
	},
	Cleanup {
		object: Object,
	},
	Operate {
		bucket: Uuid,
		key: String,
		actor: String,
		operation: Operation,
	},
	Commit {
		object: Object,
		auth: WriteAuth,
	},
}
impl Rpc for Message {
	type Response = Result<Reply, Error>;
}

pub struct WorldControl {
	system: Arc<System>,
	objects: Arc<Table<ObjectTable, TableShardedReplication>>,
	tree: db::Tree,
	serial: Mutex<()>,
	endpoint: Arc<Endpoint<Message, Self>>,
	coordinator: Option<Uuid>,
	single_node: bool,
	require_lease: bool,
	epoch: String,
}
impl WorldControl {
	pub fn new(
		system: Arc<System>,
		db: &db::Db,
		objects: Arc<Table<ObjectTable, TableShardedReplication>>,
		config: &Config,
	) -> Result<Arc<Self>, Error> {
		let coordinator = config
			.s3_api
			.world_coordinator
			.as_deref()
			.map(|s| {
				hex::decode(s)
					.ok()
					.and_then(|v| <[u8; 32]>::try_from(v.as_slice()).ok())
					.map(Uuid::from)
					.ok_or_message("world_coordinator must be a full 64-character node ID")
			})
			.transpose()?;
		let endpoint = system.netapp.endpoint("slimevault/world-control/v1".into());
		let instance = Arc::new(Self {
			tree: db.open_tree("slimevault_world_control_v1")?,
			system,
			objects,
			serial: Mutex::new(()),
			endpoint,
			coordinator,
			single_node: config.replication_factor == Some(1),
			require_lease: config.s3_api.slime_world_only && config.s3_api.world_require_lease,
			epoch: hex::encode(gen_uuid()),
		});
		instance.endpoint.set_handler(instance.clone());
		Ok(instance)
	}
	fn authority(&self) -> Result<Uuid, Error> {
		self.coordinator
			.or_else(|| self.single_node.then_some(self.system.id))
			.ok_or_else(|| {
				Error::RemoteError(
					"world_coordinator is required on every multi-node cluster node".into(),
				)
			})
	}
	pub async fn operate(
		&self,
		bucket: Uuid,
		key: String,
		actor: String,
		operation: Operation,
	) -> Result<Reply, Error> {
		self.request(Message::Operate {
			bucket,
			key,
			actor,
			operation,
		})
		.await
	}
	pub async fn check_write(
		&self,
		bucket: Uuid,
		key: String,
		auth: WriteAuth,
	) -> Result<Reply, Error> {
		self.request(Message::CheckWrite { bucket, key, auth })
			.await
	}

	pub async fn cleanup(&self, object: Object) -> Result<Reply, Error> {
		self.request(Message::Cleanup { object }).await
	}

	pub async fn commit(&self, object: Object, auth: WriteAuth) -> Result<Reply, Error> {
		self.request(Message::Commit { object, auth }).await
	}
	async fn request(&self, message: Message) -> Result<Reply, Error> {
		let authority = self.authority()?;
		if authority == self.system.id {
			return self.process(&message).await;
		}
		self.system
			.rpc_helper()
			.call(
				&self.endpoint,
				authority,
				message,
				RequestStrategy::with_priority(PRIO_NORMAL)
					.with_custom_timeout(Duration::from_secs(10)),
			)
			.await
			.map_err(|e| Error::RemoteError(format!("World coordinator unavailable: {e}")))
	}
	fn load(&self, bucket: Uuid, key: &str) -> Result<WorldState, Error> {
		let value = self.tree.get(format!("{}:{key}", hex::encode(bucket)))?;
		Ok(match value {
			Some(v) => rmp_serde::from_slice(&v)?,
			None => WorldState {
				bucket,
				key: key.into(),
				generation: 0,
				lease: None,
				frozen: false,
				protected: BTreeSet::new(),
				retention: None,
			},
		})
	}
	fn save(&self, state: &WorldState) -> Result<(), Error> {
		self.tree.insert(
			format!("{}:{}", hex::encode(state.bucket), state.key),
			rmp_serde::to_vec_named(state)?,
		)?;
		Ok(())
	}
	fn active<'a>(&self, s: &'a WorldState) -> Option<&'a Lease> {
		s.lease
			.as_ref()
			.filter(|l| l.epoch == self.epoch && l.expires_at > now_msec())
	}
	fn matches(&self, s: &WorldState, actor: &str, token: &str) -> bool {
		self.active(s)
			.is_some_and(|l| l.actor == actor && l.token == token)
	}
	async fn process(&self, message: &Message) -> Result<Reply, Error> {
		if self.authority()? != self.system.id {
			return Err(Error::RemoteError(
				"Not the configured world coordinator".into(),
			));
		}
		// Held across final table publication: takeover cannot pass an in-flight commit.
		let _serial = self.serial.lock().await;
		match message {
			Message::CheckWrite { bucket, key, auth } => {
				let state = self.load(*bucket, key)?;
				if state.frozen {
					return Ok(deny("WorldProtected", "Snapshot is immutable"));
				}
				if self.require_lease
					&& !auth
						.token
						.as_deref()
						.is_some_and(|t| self.matches(&state, &auth.actor, t))
				{
					return Ok(deny("LeaseExpired", "A current writer lease is required"));
				}
				Ok(Reply::Ok)
			}
			Message::Cleanup { object } => {
				let current = self.objects.get(&object.bucket_id, &object.key).await?;
				let aborts = object
					.versions()
					.iter()
					.filter(|v| {
						current.as_ref().is_some_and(|o| {
							o.versions()
								.iter()
								.any(|old| old.uuid == v.uuid && old.is_uploading(None))
						})
					})
					.cloned()
					.collect::<Vec<_>>();
				if !aborts.is_empty() {
					self.objects
						.insert(&Object::new(object.bucket_id, object.key.clone(), aborts))
						.await?;
				}
				Ok(Reply::Ok)
			}
			Message::Commit { object, auth } => {
				let state = self.load(object.bucket_id, &object.key)?;
				if state.frozen {
					return Ok(deny(
						"WorldProtected",
						"Snapshot is immutable; owner must explicitly unprotect it",
					));
				}
				let any_complete = object.versions().iter().any(|v| v.is_complete());
				if self.require_lease
					&& !auth
						.token
						.as_deref()
						.is_some_and(|t| self.matches(&state, &auth.actor, t))
				{
					return Ok(deny(
						"LeaseExpired",
						"A current writer lease for this key and access key is required",
					));
				}
				let current = self.objects.get(&object.bucket_id, &object.key).await?;
				if any_complete {
					if let Some(expected) = &auth.expected_revision {
						let actual = current
							.as_ref()
							.and_then(|o| o.versions().iter().rev().find(|v| v.is_complete()))
							.map(|v| hex::encode(v.uuid));
						if actual.as_deref().unwrap_or("none") != expected {
							return Ok(deny("RevisionConflict", "Current revision changed"));
						}
					}
				}
				let versions = object
					.versions()
					.iter()
					.filter(|v| {
						!(v.state == ObjectVersionState::Aborted
							&& current.as_ref().is_some_and(|o| {
								o.versions()
									.iter()
									.any(|old| old.uuid == v.uuid && old.is_complete())
							}))
					})
					.cloned()
					.collect::<Vec<_>>();
				if versions.is_empty() {
					return Ok(Reply::Ok);
				}
				let mut incoming = Object::new(object.bucket_id, object.key.clone(), versions);
				// Order every committed revision after all published history, even with clock skew.
				if any_complete {
					let timestamp = current
						.as_ref()
						.and_then(|o| o.versions().iter().map(|v| v.timestamp).max())
						.unwrap_or(0)
						.saturating_add(1)
						.max(now_msec());
					let versions = incoming
						.versions()
						.iter()
						.cloned()
						.map(|mut v| {
							if v.is_complete() {
								v.timestamp = current
									.as_ref()
									.and_then(|o| {
										o.versions().iter().find(|old| old.uuid == v.uuid)
									})
									.map(|old| old.timestamp)
									.unwrap_or(timestamp);
							}
							v
						})
						.collect();
					incoming = Object::new(incoming.bucket_id, incoming.key, versions);
				}
				self.objects.insert(&incoming).await?;
				Ok(Reply::Ok)
			}
			Message::Operate {
				bucket,
				key,
				actor,
				operation,
			} => {
				let mut state = self.load(*bucket, key)?;
				match operation {
					Operation::Acquire { ttl_ms } => {
						if state.frozen {
							return Ok(deny("WorldProtected", "Snapshot is immutable"));
						}
						if self.active(&state).is_some() {
							return Ok(deny("LeaseConflict", "Another writer owns this world"));
						}
						if !(100..=300_000).contains(ttl_ms) {
							return Ok(deny(
								"InvalidRequest",
								"ttl_ms must be between 100 and 300000",
							));
						}
						state.generation = state
							.generation
							.checked_add(1)
							.ok_or_message("Lease generation exhausted")?;
						state.lease = Some(Lease {
							token: hex::encode(gen_uuid()),
							actor: actor.clone(),
							fence: state.generation,
							expires_at: now_msec().saturating_add(*ttl_ms),
							epoch: self.epoch.clone(),
						});
					}
					Operation::Renew { token, ttl_ms } => {
						if !(100..=300_000).contains(ttl_ms) {
							return Ok(deny("InvalidRequest", "Invalid lease TTL"));
						}
						if !self.matches(&state, actor, token) {
							return Ok(deny(
								"LeaseExpired",
								"Lease has expired or belongs to another writer",
							));
						}
						state.lease.as_mut().unwrap().expires_at =
							now_msec().saturating_add(*ttl_ms);
					}
					Operation::Release { token } => {
						if !self.matches(&state, actor, token) {
							return Ok(deny(
								"LeaseExpired",
								"Lease has expired or belongs to another writer",
							));
						}
						state.lease = None;
					}
					Operation::Inspect => {
						if self.active(&state).is_none() {
							state.lease = None;
						}
						if let Some(l) = &mut state.lease {
							l.token.clear();
							l.epoch.clear();
						}
						return Ok(Reply::State(state));
					}
					Operation::Protect { revision } => {
						let object = self
							.objects
							.get(bucket, key)
							.await?
							.ok_or_message("World does not exist")?;
						if !object
							.versions()
							.iter()
							.any(|v| v.is_data() && hex::encode(v.uuid) == *revision)
						{
							return Ok(deny("NoSuchVersion", "Snapshot revision does not exist"));
						}
						state.protected.insert(revision.clone());
						state.frozen = true;
						state.lease = None;
					}
					Operation::Unprotect => {
						state.frozen = false;
						state.protected.clear();
					}
					Operation::SetRetention(policy) => {
						if policy.keep_last > 100_000 {
							return Ok(deny("InvalidRequest", "keep_last is too large"));
						}
						state.retention = Some(policy.clone());
					}
					Operation::Collect { dry_run } => {
						return self.collect(&state, None, *dry_run).await
					}
					Operation::Purge { revision } => {
						return self.collect(&state, Some(revision), false).await
					}
				}
				self.save(&state)?;
				if !matches!(
					operation,
					Operation::Acquire { .. } | Operation::Renew { .. }
				) {
					if let Some(l) = &mut state.lease {
						l.token.clear();
						l.epoch.clear();
					}
				}
				Ok(Reply::State(state))
			}
		}
	}
	async fn collect(
		&self,
		state: &WorldState,
		specific: Option<&String>,
		dry_run: bool,
	) -> Result<Reply, Error> {
		if state.frozen {
			return Ok(deny(
				"WorldProtected",
				"Protected snapshots cannot be collected",
			));
		}
		let Some(object) = self.objects.get(&state.bucket, &state.key).await? else {
			return Ok(Reply::Collected {
				revisions: vec![],
				dry_run,
			});
		};
		let mut revisions = vec![];
		let mut abort = vec![];
		let policy = state.retention.as_ref();
		for (position, version) in object
			.versions()
			.iter()
			.rev()
			.filter(|v| v.is_complete())
			.enumerate()
		{
			let id = hex::encode(version.uuid);
			let candidate = if let Some(specific) = specific {
				&id == specific
			} else {
				policy.is_some_and(|p| {
					position >= p.keep_last
						&& p.max_age_ms
							.is_none_or(|age| now_msec().saturating_sub(version.timestamp) >= age)
				})
			};
			if !candidate {
				continue;
			}
			if position == 0 || state.protected.contains(&id) {
				if specific.is_some() {
					return Ok(deny(
						"WorldProtected",
						"The latest or a protected revision cannot be purged",
					));
				}
				continue;
			}
			revisions.push(id);
			let mut v = version.clone();
			v.state = ObjectVersionState::Aborted;
			abort.push(v);
		}
		if specific.is_some() && revisions.is_empty() {
			return Ok(deny(
				"NoSuchVersion",
				"Revision does not exist or was already purged",
			));
		}
		if !dry_run && !abort.is_empty() {
			// Monotonic Aborted state replicates; delayed metadata cannot resurrect the data.
			self.objects
				.insert(&Object::new(state.bucket, state.key.clone(), abort))
				.await?;
		}
		Ok(Reply::Collected { revisions, dry_run })
	}
	pub fn spawn_retention_worker(self: &Arc<Self>, bg: &BackgroundRunner) {
		bg.spawn_worker(RetentionWorker(self.clone()));
	}
}
fn deny(code: &str, message: &str) -> Reply {
	Reply::Denied {
		code: code.into(),
		message: message.into(),
	}
}
impl EndpointHandler<Message> for WorldControl {
	async fn handle(self: &Arc<Self>, message: &Message, _from: NodeID) -> Result<Reply, Error> {
		self.process(message).await
	}
}
struct RetentionWorker(Arc<WorldControl>);
#[async_trait::async_trait]
impl Worker for RetentionWorker {
	fn name(&self) -> String {
		"SlimeVault retention".into()
	}
	async fn work(&mut self, _exit: &mut watch::Receiver<bool>) -> Result<WorkerState, Error> {
		if self.0.authority().ok() != Some(self.0.system.id) {
			return Ok(WorkerState::Idle);
		}
		let records: Vec<Vec<u8>> = self
			.0
			.tree
			.iter()?
			.map(|r| r.map(|(_, v)| v))
			.collect::<Result<_, _>>()?;
		for value in records {
			let state: WorldState = rmp_serde::from_slice(&value)?;
			if state.retention.is_some() && !state.frozen {
				self.0
					.operate(
						state.bucket,
						state.key,
						"retention-worker".into(),
						Operation::Collect { dry_run: false },
					)
					.await?;
			}
		}
		Ok(WorkerState::Idle)
	}
	async fn wait_for_work(&mut self) -> WorkerState {
		tokio::time::sleep(Duration::from_secs(30)).await;
		WorkerState::Busy
	}
}
