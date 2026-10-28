//! SRF 13 structural validation, based on the ASP serializer/reader at 031913915008.
//! Does not claim Minecraft semantic compatibility or perform world upgrades.
use crate::{encryption::EncryptionParams, error::*};
use futures::TryStreamExt;
use garage_model::{garage::Garage, s3::object_table::*};
use garage_util::data::gen_uuid;
use std::fs::{File, OpenOptions};
use std::io::{Cursor, Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

static VALIDATION_PERMITS: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(2);

pub type SharedValidator = Arc<Mutex<Option<Validator>>>;
pub struct Validator {
	path: PathBuf,
	file: File,
	length: u64,
	max_bytes: u64,
	max_expanded: u64,
}
impl Drop for Validator {
	fn drop(&mut self) {
		let _ = std::fs::remove_file(&self.path);
	}
}
impl Validator {
	pub fn new(garage: &Garage) -> Result<Self, Error> {
		let dir = garage.config.metadata_dir.join("world-validation");
		std::fs::create_dir_all(&dir).ok_or_internal_error("Create validation directory")?;
		let path = dir.join(hex::encode(gen_uuid()));
		let file = OpenOptions::new()
			.create_new(true)
			.write(true)
			.read(true)
			.open(&path)
			.ok_or_internal_error("Create validation spool")?;
		#[cfg(unix)]
		std::fs::remove_file(&path).ok_or_internal_error("Unlink validation spool")?;
		Ok(Self {
			path,
			file,
			length: 0,
			max_bytes: garage.config.s3_api.world_max_bytes,
			max_expanded: garage.config.s3_api.world_max_expanded_bytes,
		})
	}
	pub fn write(&mut self, bytes: &[u8]) -> Result<(), Error> {
		self.length = self.length.saturating_add(bytes.len() as u64);
		if self.length > self.max_bytes {
			return Err(Error::bad_request("SlimeWorld exceeds world_max_bytes"));
		}
		self.file
			.write_all(bytes)
			.ok_or_internal_error("Write validation spool")?;
		Ok(())
	}
	pub fn finish(mut self) -> Result<(), Error> {
		self.file
			.flush()
			.ok_or_internal_error("Flush validation spool")?;
		self.file
			.seek(SeekFrom::Start(0))
			.ok_or_internal_error("Rewind validation spool")?;
		validate(&mut self.file, self.max_expanded)
			.map_err(|e| Error::bad_request(format!("Invalid SRF 13 world: {e}")))
	}
}
pub fn enabled(garage: &Garage, key: &str) -> bool {
	key.ends_with(".slime")
		&& garage.config.s3_api.slime_world_only
		&& garage.config.s3_api.world_validate_format
}
pub async fn finish(shared: SharedValidator) -> Result<(), Error> {
	let validator = shared.lock().unwrap().take();
	if let Some(v) = validator {
		let _permit = VALIDATION_PERMITS
			.acquire()
			.await
			.ok_or_internal_error("World validation permit")?;
		tokio::task::spawn_blocking(move || v.finish())
			.await
			.ok_or_internal_error("World validation task")??;
	}
	Ok(())
}
pub async fn validate_object(
	garage: Arc<Garage>,
	version: &ObjectVersion,
	data: &ObjectVersionData,
	encryption: EncryptionParams,
) -> Result<(), Error> {
	let mut validator = Validator::new(&garage)?;
	let mut stream = crate::get::full_object_byte_stream(garage, version, data, encryption);
	while let Some(bytes) = stream
		.try_next()
		.await
		.ok_or_bad_request("Read world for validation")?
	{
		validator.write(&bytes)?;
	}
	let _permit = VALIDATION_PERMITS
		.acquire()
		.await
		.ok_or_internal_error("World validation permit")?;
	tokio::task::spawn_blocking(move || validator.finish())
		.await
		.ok_or_internal_error("World validation task")??;
	Ok(())
}
type FResult<T> = Result<T, String>;
fn exact<R: Read>(r: &mut R, bytes: &mut [u8]) -> FResult<()> {
	r.read_exact(bytes).map_err(|_| "truncated data".into())
}
fn byte<R: Read>(r: &mut R) -> FResult<u8> {
	let mut b = [0];
	exact(r, &mut b)?;
	Ok(b[0])
}
fn int<R: Read>(r: &mut R) -> FResult<u32> {
	let mut b = [0; 4];
	exact(r, &mut b)?;
	Ok(u32::from_be_bytes(b))
}
fn skip<R: Read>(r: &mut R, n: u64) -> FResult<()> {
	if std::io::copy(&mut r.take(n), &mut std::io::sink()).map_err(|_| "read failure")? != n {
		return Err("truncated field".into());
	}
	Ok(())
}
fn string<R: Read>(r: &mut R) -> FResult<()> {
	let mut n = [0; 2];
	exact(r, &mut n)?;
	skip(r, u16::from_be_bytes(n) as u64)
}
fn nbt_payload<R: Read>(r: &mut R, tag: u8, depth: u8, budget: &mut u64) -> FResult<()> {
	if depth > 64 || *budget == 0 {
		return Err("NBT nesting or element limit exceeded".into());
	}
	*budget -= 1;
	match tag {
		1 => skip(r, 1),
		2 => skip(r, 2),
		3 | 5 => skip(r, 4),
		4 | 6 => skip(r, 8),
		7 | 11 | 12 => {
			let n = int(r)?;
			if n > i32::MAX as u32 {
				return Err("negative NBT length".into());
			}
			let width = if tag == 11 {
				4
			} else if tag == 12 {
				8
			} else {
				1
			};
			skip(r, n as u64 * width)
		}
		8 => string(r),
		9 => {
			let element = byte(r)?;
			let n = int(r)?;
			if n as u64 > *budget {
				return Err("NBT list too large".into());
			}
			for _ in 0..n {
				nbt_payload(r, element, depth + 1, budget)?;
			}
			Ok(())
		}
		10 => {
			loop {
				let child = byte(r)?;
				if child == 0 {
					break;
				}
				string(r)?;
				nbt_payload(r, child, depth + 1, budget)?;
			}
			Ok(())
		}
		_ => Err("unknown NBT tag".into()),
	}
}
fn nbt(bytes: &[u8]) -> FResult<()> {
	let mut r = Cursor::new(bytes);
	if byte(&mut r)? != 10 {
		return Err("NBT root is not a compound".into());
	}
	string(&mut r)?;
	nbt_payload(&mut r, 10, 0, &mut 10_000_000)?;
	if r.position() != bytes.len() as u64 {
		return Err("NBT trailing data".into());
	}
	Ok(())
}
fn compound(r: &mut Cursor<Vec<u8>>) -> FResult<()> {
	let n = int(r)? as usize;
	if n == 0 {
		return Ok(());
	}
	let start = r.position() as usize;
	let end = start
		.checked_add(n)
		.filter(|end| *end <= r.get_ref().len())
		.ok_or("truncated compound")?;
	nbt(&r.get_ref()[start..end])?;
	r.set_position(end as u64);
	Ok(())
}
fn compressed<R: Read>(r: &mut R, remaining: &mut u64) -> FResult<Vec<u8>> {
	let size = int(r)?;
	let expanded = int(r)?;
	if size > i32::MAX as u32 || expanded > i32::MAX as u32 || expanded as u64 > *remaining {
		return Err("compressed section size limit exceeded".into());
	}
	let mut data = Vec::new();
	let mut limited = r.take(size as u64);
	{
		let decoder = zstd::stream::read::Decoder::new(&mut limited)
			.map_err(|e| format!("invalid zstd: {e}"))?;
		decoder
			.take(remaining.saturating_add(1))
			.read_to_end(&mut data)
			.map_err(|e| format!("invalid zstd: {e}"))?;
	}
	if limited.limit() != 0 {
		return Err("truncated compressed section".into());
	}
	if data.len() as u64 > *remaining || data.len() != expanded as usize {
		return Err("decompressed size mismatch".into());
	}
	*remaining -= data.len() as u64;
	Ok(data)
}
fn validate<R: Read>(r: &mut R, mut remaining: u64) -> FResult<()> {
	let mut magic = [0; 2];
	exact(r, &mut magic)?;
	if magic != [0xb1, 0x0b] {
		return Err("invalid SlimeWorld magic".into());
	}
	if byte(r)? != 13 {
		return Err("unsupported SRF version (expected 13)".into());
	}
	let data_version = int(r)?;
	if data_version > i32::MAX as u32 {
		return Err("invalid Minecraft data version".into());
	}
	let flags = byte(r)?;
	let mut chunks = Cursor::new(compressed(r, &mut remaining)?);
	let count = int(&mut chunks)?;
	if count > 1_000_000 {
		return Err("chunk count limit exceeded".into());
	}
	let mut positions = std::collections::BTreeSet::new();
	for _ in 0..count {
		let x = int(&mut chunks)?;
		let z = int(&mut chunks)?;
		if !positions.insert((x, z)) {
			return Err("duplicate chunk coordinates".into());
		}
		let sections = int(&mut chunks)?;
		if sections > 1024 {
			return Err("section count limit exceeded".into());
		}
		for _ in 0..sections {
			let light = byte(&mut chunks)?;
			if light & !3 != 0 {
				return Err("unknown section flags".into());
			}
			if light & 1 != 0 {
				skip(&mut chunks, 2048)?;
			}
			if light & 2 != 0 {
				skip(&mut chunks, 2048)?;
			}
			compound(&mut chunks)?;
			compound(&mut chunks)?;
		}
		compound(&mut chunks)?;
		for bit in 0..8 {
			if flags & (1 << bit) != 0 {
				compound(&mut chunks)?;
			}
		}
		for _ in 0..3 {
			compound(&mut chunks)?;
		}
	}
	if chunks.position() != chunks.get_ref().len() as u64 {
		return Err("chunk stream trailing data".into());
	}
	nbt(&compressed(r, &mut remaining)?)?;
	let mut extra = [0];
	if r.read(&mut extra).map_err(|_| "read failure")? != 0 {
		return Err("world trailing data".into());
	}
	Ok(())
}
#[cfg(test)]
mod tests {
	use super::*;
	fn fixture() -> Vec<u8> {
		let mut out = vec![0xb1, 0x0b, 13, 0, 0, 0x10, 0, 0];
		for data in [&[0u8; 4][..], &[10, 0, 0, 0][..]] {
			let compressed = zstd::stream::encode_all(data, 1).unwrap();
			out.extend((compressed.len() as u32).to_be_bytes());
			out.extend((data.len() as u32).to_be_bytes());
			out.extend(compressed);
		}
		out
	}
	#[test]
	fn accepts_worlds_written_by_asp_26_3() {
		let fixtures: [&[u8]; 2] = [
			include_bytes!("../../../tests/fixtures/asp-26.3/asp-world-v1.slime"),
			include_bytes!("../../../tests/fixtures/asp-26.3/asp-world-v2.slime"),
		];
		for mut world in fixtures {
			validate(&mut world, 1024 * 1024).unwrap();
		}
	}
	#[test]
	fn valid_empty_world() {
		validate(&mut fixture().as_slice(), 1024).unwrap();
	}
	#[test]
	fn rejects_corrupt_and_oversized_worlds() {
		let good = fixture();
		for n in 0..good.len() {
			assert!(validate(&mut &good[..n], 1024).is_err());
		}
		let mut bad = good.clone();
		bad.push(0);
		assert!(validate(&mut bad.as_slice(), 1024).is_err());
		assert!(validate(&mut good.as_slice(), 1).is_err());
	}
}
