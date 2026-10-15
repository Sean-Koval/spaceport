"use strict";
// One process, local POSIX filesystem only. No stale-lock recovery or automatic initialization.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const C = require("./runtime-contract.cjs");
const ZERO = "0".repeat(64);
const unavailable = () =>
  new Error("BigQuery ledger is unavailable or corrupt.");
function directoryInfo(directory) {
  if (
    process.platform === "win32" ||
    typeof process.getuid !== "function" ||
    !path.isAbsolute(directory) ||
    path.resolve(directory) !== directory ||
    fs.realpathSync(directory) !== directory
  )
    throw unavailable();
  const s = fs.lstatSync(directory);
  if (
    !s.isDirectory() ||
    s.uid !== process.getuid() ||
    (s.mode & 0o777) !== 0o700
  )
    throw unavailable();
  return s;
}
function sameFile(a, b) {
  return a.ino === b.ino && a.dev === b.dev;
}
function openPrivate(filename, flags = fs.constants.O_RDONLY) {
  const fd = fs.openSync(
    filename,
    flags | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
    0o600,
  );
  try {
    const s = fs.fstatSync(fd);
    if (
      !s.isFile() ||
      s.uid !== process.getuid() ||
      (s.mode & 0o777) !== 0o600 ||
      s.nlink !== 1
    )
      throw unavailable();
    return fd;
  } catch (e) {
    fs.closeSync(fd);
    throw e;
  }
}
function readPrivate(filename, max) {
  const fd = openPrivate(filename);
  try {
    const size = fs.fstatSync(fd).size;
    if (size > max) throw unavailable();
    const b = Buffer.alloc(size);
    let offset = 0;
    while (offset < size) {
      const n = fs.readSync(fd, b, offset, size - offset, offset);
      if (!n) throw unavailable();
      offset += n;
    }
    return b;
  } finally {
    fs.closeSync(fd);
  }
}
function writeAll(fd, buffer) {
  let n = 0;
  while (n < buffer.length) {
    const wrote = fs.writeSync(fd, buffer, n, buffer.length - n);
    if (!wrote) throw unavailable();
    n += wrote;
  }
}
function createFile(filename, content) {
  const fd = openPrivate(
    filename,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
  );
  try {
    writeAll(fd, Buffer.from(content));
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}
function syncDirectory(directory) {
  const fd = fs.openSync(
    directory,
    fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
  );
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}
function initializeLedger(directory, options) {
  C.exact(options, ["deploymentId", "ledgerId", "limits"]);
  const id = C.identity({
    deploymentId: options?.deploymentId,
    ledgerId: options?.ledgerId,
  });
  const bounds = C.limits(options?.limits);
  try {
    // Explicit preparation only, in a new private directory. Runtime never calls this.
    directoryInfo(path.dirname(directory));
    fs.mkdirSync(directory, { mode: 0o700 });
    directoryInfo(directory);
    createFile(
      path.join(directory, "manifest"),
      C.canonical({ version: 1, ...id, limits: bounds }),
    );
    createFile(path.join(directory, "journal"), "");
    createFile(
      path.join(directory, "head"),
      C.canonical({
        version: 1,
        ledgerId: id.ledgerId,
        seq: 0,
        hash: ZERO,
        bytes: 0,
      }),
    );
    syncDirectory(directory);
    syncDirectory(path.dirname(directory));
  } catch (_) {
    throw unavailable();
  }
}
class AdmissionLedger {
  #directory;
  #identity;
  #limits;
  #directoryStat;
  #journalFd;
  #journalStat;
  #journalStamp;
  #manifest;
  #lockFd;
  #lockStat;
  #head;
  #state = new Map();
  #closed = false;
  #poisoned = false;
  constructor(directory, expectedIdentity) {
    this.#identity = C.identity(expectedIdentity);
    this.#directory = directory;
    try {
      this.#directoryStat = directoryInfo(directory);
      const manifest = JSON.parse(
        readPrivate(path.join(directory, "manifest"), 4096),
      );
      C.exact(manifest, ["version", "deploymentId", "ledgerId", "limits"]);
      if (
        manifest.version !== 1 ||
        manifest.deploymentId !== this.#identity.deploymentId ||
        manifest.ledgerId !== this.#identity.ledgerId
      )
        throw unavailable();
      this.#limits = C.limits(manifest.limits);
      this.#manifest = C.canonical(manifest);
      // Open exclusive lock before recovery. Invalid recovery deliberately retains this lock.
      try {
        this.#lockFd = openPrivate(
          path.join(directory, "lock"),
          fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
        );
      } catch (error) {
        if (error.code === "EEXIST")
          throw new Error("BigQuery ledger requires exclusive storage.");
        throw error;
      }
      this.#lockStat = fs.fstatSync(this.#lockFd);
      writeAll(
        this.#lockFd,
        Buffer.from(
          C.canonical({ pid: process.pid, nonce: crypto.randomUUID() }),
        ),
      );
      fs.fsyncSync(this.#lockFd);
      syncDirectory(directory);
      this.#head = JSON.parse(readPrivate(path.join(directory, "head"), 1024));
      C.exact(this.#head, ["version", "ledgerId", "seq", "hash", "bytes"]);
      if (
        this.#head.version !== 1 ||
        this.#head.ledgerId !== this.#identity.ledgerId
      )
        throw unavailable();
      C.integer(this.#head.seq, 0, 100000);
      C.text(this.#head.hash, C.HEX);
      C.integer(this.#head.bytes, 0, this.#limits.maxJournalBytes);
      const journal = readPrivate(
        path.join(directory, "journal"),
        this.#limits.maxJournalBytes,
      );
      let offset = 0,
        seq = 0,
        previous = ZERO;
      while (offset < journal.length) {
        if (offset + 4 > journal.length) throw unavailable();
        const length = journal.readUInt32BE(offset);
        offset += 4;
        if (
          length > this.#limits.maxRecordBytes ||
          offset + length > journal.length
        )
          throw unavailable();
        const record = JSON.parse(
          journal.subarray(offset, offset + length).toString("utf8"),
        );
        offset += length;
        C.exact(record, ["seq", "prev", "event", "hash"]);
        if (
          record.seq !== ++seq ||
          record.prev !== previous ||
          record.hash !== C.digest({ seq, prev: previous, event: record.event })
        )
          throw unavailable();
        this.#apply(record.event);
        previous = record.hash;
      }
      if (
        this.#head.seq !== seq ||
        this.#head.hash !== previous ||
        this.#head.bytes !== offset
      )
        throw unavailable();
      this.#journalFd = openPrivate(
        path.join(directory, "journal"),
        fs.constants.O_WRONLY | fs.constants.O_APPEND,
      );
      this.#journalStat = fs.fstatSync(this.#journalFd);
      this.#journalStamp = fs.fstatSync(this.#journalFd, { bigint: true });
    } catch (error) {
      this.#poisoned = true;
      for (const fd of [this.#journalFd, this.#lockFd])
        if (fd !== undefined)
          try {
            fs.closeSync(fd);
          } catch {}
      if (error.message === "BigQuery ledger requires exclusive storage.")
        throw error;
      throw unavailable();
    }
  }
  #check() {
    if (this.#closed || this.#poisoned) throw unavailable();
    try {
      if (
        !sameFile(directoryInfo(this.#directory), this.#directoryStat) ||
        !sameFile(
          fs.lstatSync(path.join(this.#directory, "journal")),
          this.#journalStat,
        ) ||
        (fs.fstatSync(this.#journalFd).mode & 0o777) !== 0o600 ||
        fs.fstatSync(this.#journalFd).nlink !== 1 ||
        !sameFile(
          fs.lstatSync(path.join(this.#directory, "lock")),
          this.#lockStat,
        ) ||
        fs.fstatSync(this.#journalFd).size !== this.#head.bytes ||
        fs.fstatSync(this.#journalFd, { bigint: true }).mtimeNs !==
          this.#journalStamp.mtimeNs ||
        fs.fstatSync(this.#journalFd, { bigint: true }).ctimeNs !==
          this.#journalStamp.ctimeNs ||
        readPrivate(path.join(this.#directory, "manifest"), 4096).toString() !==
          this.#manifest ||
        readPrivate(path.join(this.#directory, "head"), 1024).toString() !==
          C.canonical(this.#head)
      )
        throw unavailable();
    } catch (_) {
      this.#poisoned = true;
      throw unavailable();
    }
  }
  #append(event) {
    this.#check();
    const base = { seq: this.#head.seq + 1, prev: this.#head.hash, event };
    const record = { ...base, hash: C.digest(base) };
    const data = Buffer.from(C.canonical(record));
    const frame = Buffer.alloc(data.length + 4);
    frame.writeUInt32BE(data.length);
    data.copy(frame, 4);
    if (
      data.length > this.#limits.maxRecordBytes ||
      this.#head.bytes + frame.length > this.#limits.maxJournalBytes ||
      base.seq > 100000
    )
      throw new Error("BigQuery ledger storage capacity is exhausted.");
    const head = {
      version: 1,
      ledgerId: this.#identity.ledgerId,
      seq: base.seq,
      hash: record.hash,
      bytes: this.#head.bytes + frame.length,
    };
    try {
      writeAll(this.#journalFd, frame);
      fs.fsyncSync(this.#journalFd);
      const temporary = path.join(this.#directory, "head.next");
      createFile(temporary, C.canonical(head));
      fs.renameSync(temporary, path.join(this.#directory, "head"));
      syncDirectory(this.#directory);
      this.#head = head;
      this.#journalStamp = fs.fstatSync(this.#journalFd, { bigint: true });
      this.#apply(event);
    } catch (_) {
      this.#poisoned = true;
      throw unavailable();
    }
  }
  #apply(event) {
    C.exact(
      event,
      event.kind === "register"
        ? ["kind", "permit"]
        : event.kind === "generation"
          ? ["kind", "executionId", "generation", "deadlineMs"]
          : event.kind === "admit"
            ? ["kind", "executionId", "job"]
            : event.kind === "observe"
              ? ["kind", "executionId", "jobKey", "status"]
              : ["kind", "executionId"],
    );
    if (event.kind === "register") {
      const permit = C.permit(event.permit);
      if (
        this.#state.has(permit.executionId) ||
        this.#state.size >= this.#limits.maxExecutions ||
        permit.maxJobs > this.#limits.maxJobsPerExecution
      )
        throw unavailable();
      this.#state.set(permit.executionId, {
        permit,
        jobs: new Map(),
        sealed: false,
        reservedBytes: 0n,
      });
      return;
    }
    const execution = this.#state.get(event.executionId);
    if (!execution) throw unavailable();
    if (event.kind === "generation") {
      C.integer(event.generation, 1, 2147483647);
      C.integer(event.deadlineMs, 1, execution.permit.deadlineMs);
      if (event.generation < execution.permit.generation) throw unavailable();
      execution.permit.generation = event.generation;
      execution.permit.deadlineMs = event.deadlineMs;
      return;
    }
    if (event.kind === "admit") {
      const j = event.job;
      C.exact(j, ["key", "jobId", "config", "configDigest", "status"]);
      C.text(j.key, C.HEX);
      C.text(j.jobId, /^sp_[a-f0-9]{64}$/);
      C.jobConfig(j.config);
      C.text(j.configDigest, C.HEX);
      if (
        j.configDigest !== C.digest(j.config) ||
        j.jobId !== this.#jobId(event.executionId, j.key) ||
        j.status !== "submit_intent" ||
        execution.sealed ||
        execution.jobs.has(j.key) ||
        execution.jobs.size >= execution.permit.maxJobs ||
        j.config.maximumBytesBilled !== execution.permit.perJobBytes
      )
        throw unavailable();
      const reserved =
        execution.reservedBytes + BigInt(j.config.maximumBytesBilled);
      if (reserved > BigInt(execution.permit.totalBytes)) throw unavailable();
      execution.jobs.set(j.key, C.copy(j));
      execution.reservedBytes = reserved;
      return;
    }
    if (event.kind === "observe") {
      const job = execution.jobs.get(event.jobKey);
      if (
        !job ||
        !["running", "cancel_requested", "done"].includes(event.status) ||
        job.status === "done" ||
        job.status === event.status
      )
        throw unavailable();
      if (job.status === "cancel_requested" && event.status === "running")
        throw unavailable();
      job.status = event.status;
      return;
    }
    if (event.kind === "seal") {
      if (execution.sealed) throw unavailable();
      execution.sealed = true;
      return;
    }
    throw unavailable();
  }
  #jobId(executionId, key) {
    return "sp_" + C.digest({ ...this.#identity, executionId, key });
  }
  #execution(executionId, generation) {
    this.#check();
    const state = this.#state.get(executionId);
    if (
      !state ||
      (generation !== undefined && generation !== state.permit.generation)
    )
      throw new Error("BigQuery execution attempt is unavailable.");
    return state;
  }
  register(value, nowMs) {
    this.#check();
    const p = C.permit(value);
    C.integer(nowMs, 0, 8640000000000000);
    if (p.deadlineMs - nowMs > 120000) C.fail();
    if (nowMs >= p.deadlineMs)
      throw new Error("BigQuery execution deadline elapsed.");
    const old = this.#state.get(p.executionId);
    if (old) {
      const fixed = (x) =>
        Object.fromEntries(
          Object.entries(x).filter(
            ([k]) => !["generation", "deadlineMs"].includes(k),
          ),
        );
      if (C.canonical(fixed(old.permit)) !== C.canonical(fixed(p)))
        throw new Error("BigQuery execution identity changed.");
      if (p.generation < old.permit.generation)
        throw new Error("BigQuery execution attempt is unavailable.");
      const deadlineMs = Math.min(p.deadlineMs, old.permit.deadlineMs);
      if (
        p.generation !== old.permit.generation ||
        deadlineMs !== old.permit.deadlineMs
      )
        this.#append({
          kind: "generation",
          executionId: p.executionId,
          generation: p.generation,
          deadlineMs,
        });
    } else {
      if (
        this.#state.size >= this.#limits.maxExecutions ||
        p.maxJobs > this.#limits.maxJobsPerExecution
      )
        throw new Error("BigQuery ledger execution capacity is exhausted.");
      this.#append({ kind: "register", permit: p });
    }
    return C.copy(this.#state.get(p.executionId).permit);
  }
  attempt(executionId, generation) {
    return C.copy(this.#execution(executionId, generation).permit);
  }
  admit(executionId, generation, key, value, nowMs) {
    const state = this.#execution(executionId, generation);
    C.text(key, C.HEX);
    const config = C.jobConfig(value);
    C.integer(nowMs, 0, 8640000000000000);
    if (nowMs >= state.permit.deadlineMs)
      throw new Error("BigQuery execution deadline elapsed.");
    const prior = state.jobs.get(key);
    if (prior) {
      const stable = (c) => ({ ...c, jobTimeoutMs: 0 });
      if (C.canonical(stable(prior.config)) !== C.canonical(stable(config)))
        throw new Error("BigQuery admitted job identity changed.");
      return { submit: false, job: C.copy(prior) };
    }
    if (state.sealed) throw new Error("BigQuery execution is sealed.");
    if (
      config.maximumBytesBilled !== state.permit.perJobBytes ||
      state.jobs.size >= state.permit.maxJobs ||
      state.reservedBytes + BigInt(config.maximumBytesBilled) >
        BigInt(state.permit.totalBytes)
    )
      throw new Error("BigQuery execution budget is exhausted.");
    const job = {
      key,
      jobId: this.#jobId(executionId, key),
      config,
      configDigest: C.digest(config),
      status: "submit_intent",
    };
    this.#append({ kind: "admit", executionId, job });
    return { submit: true, job: C.copy(job) };
  }
  observe(executionId, jobKey, status) {
    const state = this.#execution(executionId);
    C.text(jobKey, C.HEX);
    const job = state.jobs.get(jobKey);
    if (!job || !["running", "cancel_requested", "done"].includes(status))
      C.fail();
    if (
      job.status === status ||
      (job.status === "cancel_requested" && status === "running")
    )
      return;
    if (job.status === "done")
      throw new Error("BigQuery job terminal state cannot regress.");
    this.#append({ kind: "observe", executionId, jobKey, status });
  }
  status(executionId) {
    const state = this.#execution(executionId);
    return {
      sealed: state.sealed,
      allJobsDone:
        state.sealed &&
        [...state.jobs.values()].every((j) => j.status === "done"),
      jobs: state.jobs.size,
      reservedBytes: state.reservedBytes.toString(),
    };
  }
  seal(executionId, generation) {
    const state = this.#execution(executionId, generation);
    if (!state.sealed) this.#append({ kind: "seal", executionId });
    const { sealed, allJobsDone, jobs } = this.status(executionId);
    return { sealed, allJobsDone, jobs };
  }
  close() {
    if (this.#closed) return;
    this.#closed = true;
    for (const fd of [this.#journalFd, this.#lockFd])
      if (fd !== undefined)
        try {
          fs.closeSync(fd);
        } catch {
          this.#poisoned = true;
        }
    if (!this.#poisoned) {
      try {
        if (
          !sameFile(
            fs.lstatSync(path.join(this.#directory, "lock")),
            this.#lockStat,
          )
        )
          throw unavailable();
        fs.unlinkSync(path.join(this.#directory, "lock"));
        syncDirectory(this.#directory);
      } catch (_) {
        this.#poisoned = true;
        throw unavailable();
      }
    }
  }
}
module.exports = { AdmissionLedger, initializeLedger };
