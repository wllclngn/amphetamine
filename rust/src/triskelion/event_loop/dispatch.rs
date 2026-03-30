// Request dispatch and auto-stub logic

use super::*;
#[allow(unused_variables)]

impl EventLoop {

    /// Read CLOCK_MONOTONIC_RAW in nanoseconds (vDSO, no syscall overhead).
    #[inline(always)]
    fn clock_raw_ns() -> u64 {
        let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
        unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC_RAW, &mut ts); }
        ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
    }

    /// Generic fallback for unimplemented opcodes.
    /// Returns a protocol-correct zeroed reply: error=0, reply_size=0, all fields zero.
    /// Wine reads 64 bytes for every reply. Zero reply_size means no vararg data.
    /// This is "success, nothing to report" — empty lists, null handles, default state.
    pub(crate) fn generic_fallback(&mut self, idx: usize, client_fd: i32, _buf: &[u8]) -> Reply {
        let meta = &OPCODE_META[idx];
        self.intel.record_stub(idx);
        // Return NOT_IMPLEMENTED so the auto-stub logic can decide whether to
        // convert to success (normal opcodes) or pass through (esync/fsync).
        reply_fixed(&ReplyHeader { error: 0xC000_0002, reply_size: 0 })
    }

    pub(crate) fn dispatch(&mut self, client_fd: RawFd, header: &RequestHeader, buf: &[u8]) -> Reply {
        // Dynamic protocol remap: translate client's opcode number to our RequestCode.
        // When the client uses a different Wine/Proton version, opcode numbers differ.
        let resolved = if self.protocol_remap.is_identity {
            RequestCode::from_i32(header.req)
        } else {
            self.protocol_remap.resolve(header.req)
        };
        match resolved {
            Some(code) => {
                // Use OUR opcode index for stats (consistent across client versions)
                let idx = code as i32 as usize;
                if idx < MAX_OPCODES {
                    self.opcode_counts[idx] += 1;
                }
                self.total_requests += 1;
                self.intel.record_call(idx);
                if self.total_requests <= 100 || self.total_requests % 500 == 0 || OPCODE_META[idx].name.starts_with("init_") || OPCODE_META[idx].name.starts_with("new_") {
                    log_info!("[trace] #{} {} fd={}", self.total_requests, code.as_str(), client_fd);
                }
                let t0 = Self::clock_raw_ns();
                let mut reply = {
                    let self_ptr = self as *mut EventLoop;
                    let buf_ptr = buf as *const [u8];
                    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        let el = unsafe { &mut *self_ptr };
                        let b = unsafe { &*buf_ptr };
                        dispatch_request(code, el, client_fd as i32, b)
                    })) {
                        Ok(r) => r,
                        Err(_) => {
                            log_error!("PANIC in handler {code:?} fd={client_fd} — returning STATUS_INTERNAL_ERROR");
                            reply_fixed(&ReplyHeader { error: 0xC00000E5, reply_size: 0 })
                        }
                    }
                };
                // Auto-stub: if handler returned STATUS_NOT_IMPLEMENTED,
                // replace with a protocol-correct zeroed reply (error=0).
                // OPCODE_META provides the opcode name and structure info.
                let err = match &reply {
                    Reply::Fixed { buf, len } if *len >= 4 => u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]),
                    Reply::Vararg(v) if v.len() >= 4 => u32::from_le_bytes([v[0], v[1], v[2], v[3]]),
                    _ => 0,
                };
                if err == 0xC000_0002 {
                    let meta = &OPCODE_META[idx];
                    // Don't auto-stub esync/fsync — client NEEDS NOT_IMPLEMENTED
                    // to fall back to server-side waits.
                    let is_esync_fsync = matches!(meta.name,
                        "create_esync" | "open_esync" | "get_esync_fd" | "esync_msgwait" |
                        "get_esync_apc_fd" | "create_fsync" | "open_fsync" | "get_fsync_idx" |
                        "fsync_msgwait" | "get_fsync_apc_idx" | "fsync_free_shm_idx");
                    if !is_esync_fsync {
                        self.intel.record_stub(idx);
                        // Opcodes that return handles in their reply MUST NOT be
                        // auto-stubbed to zeroed success — Wine stores handle=0 and
                        // crashes later. Pass NOT_IMPLEMENTED so Wine falls back.
                        let has_handle_reply = matches!(meta.name,
                            "debug_process" | "create_debug_obj" | "create_device_manager" |
                            "open_process" | "open_thread" | "open_file_object" |
                            "create_keyed_event" | "open_keyed_event" |
                            "allocate_reserve_object");
                        if has_handle_reply {
                            log_warn!("[stub] {} (opcode {}) → NOT_IMPLEMENTED fd={client_fd}",
                                meta.name, idx);
                        } else if self.intel.should_auto_stub(idx, meta.has_vararg_reply) {
                            let hdr = ReplyHeader { error: 0, reply_size: 0 };
                            let mut stub_buf = [0u8; 64];
                            unsafe {
                                std::ptr::copy_nonoverlapping(
                                    &hdr as *const _ as *const u8,
                                    stub_buf.as_mut_ptr(),
                                    std::mem::size_of::<ReplyHeader>(),
                                );
                            }
                            log_warn!("[auto-stub] {} (opcode {}) → zeroed success fd={client_fd}",
                                meta.name, idx);
                            reply = Reply::Fixed { buf: stub_buf, len: 64 };
                        } else {
                            log_warn!("[auto-stub] {} (opcode {}) → NOT_IMPLEMENTED fd={client_fd}",
                                meta.name, idx);
                        }
                    }
                } else if err != 0 && err != 0x8000001a && err != 0xc0000034 && err != 0x40000000
                       && err != 0x102 && err != 0x103 {
                    log_warn!("!! {code:?} err=0x{err:08x} fd={client_fd}");
                }
                let elapsed_ns = Self::clock_raw_ns() - t0;
                if idx < MAX_OPCODES {
                    self.opcode_time_ns[idx] += elapsed_ns;
                }
                self.total_dispatch_ns += elapsed_ns;
                reply
            }
            None => {
                log_warn!("unknown opcode {} fd={client_fd}", header.req);
                reply_fixed(&ReplyHeader { error: 0xC0000002, reply_size: 0 })
            }
        }
    }


    pub(super) fn dump_opcode_stats(&self) {
        log_info!("opcode stats ({} total requests):", self.total_requests);

        let mut sorted: Vec<(usize, u64)> = self.opcode_counts.iter()
            .enumerate()
            .filter(|(_, c)| **c > 0)
            .map(|(i, c)| (i, *c))
            .collect();
        sorted.sort_by(|a, b| b.1.cmp(&a.1));

        for (idx, count) in &sorted {
            let name = RequestCode::from_i32(*idx as i32)
                .map(|c| c.as_str())
                .unwrap_or("unknown");
            let pct = *count as f64 / self.total_requests as f64 * 100.0;
            log_info!("  {count:>8}  {pct:>5.1}%  {name}");
        }

        // Timing summary
        let uptime = self.start_time.elapsed();
        let avg_ns = if self.total_requests > 0 { self.total_dispatch_ns / self.total_requests } else { 0 };
        let rps = if uptime.as_secs_f64() > 0.0 { self.total_requests as f64 / uptime.as_secs_f64() } else { 0.0 };
        log_info!("timing: {:.3}ms total dispatch | {}ns avg/request | {:.0} req/s | {} requests in {:.3}s",
            self.total_dispatch_ns as f64 / 1_000_000.0,
            avg_ns,
            rps,
            self.total_requests,
            uptime.as_secs_f64());

        // Write to file for later analysis
        let log_dir = "/tmp/quark";
        let _ = std::fs::create_dir_all(log_dir);
        let log_path = format!("{log_dir}/triskelion_opcode_stats.txt");
        if let Ok(mut f) = std::fs::File::create(&log_path) {
            use std::io::Write;
            let _ = writeln!(f, "triskelion opcode stats ({} total, CLOCK_MONOTONIC_RAW)", self.total_requests);
            let _ = writeln!(f, "total_dispatch_ns: {}", self.total_dispatch_ns);
            let _ = writeln!(f, "avg_ns_per_request: {}", avg_ns);
            let _ = writeln!(f, "requests_per_sec: {:.0}", rps);
            let _ = writeln!(f, "uptime_ms: {}", uptime.as_millis());
            let _ = writeln!(f, "---");
            for (idx, count) in &sorted {
                let name = RequestCode::from_i32(*idx as i32)
                    .map(|c| c.as_str())
                    .unwrap_or("unknown");
                let time_ns = self.opcode_time_ns[*idx];
                let avg = if *count > 0 { time_ns / count } else { 0 };
                let _ = writeln!(f, "{count:>8}  {time_ns:>12}ns  {avg:>8}ns/call  {name}");
            }
            log_info!("stats written to {log_path}");
        }
    }


}
