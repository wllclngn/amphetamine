// Process lifecycle handlers

use super::*;
#[allow(unused_variables)]

impl EventLoop {
    pub(crate) fn handle_new_process(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let caller_pid = self.client_pid(client_fd as RawFd);
        log_info!("NEW_PROCESS_ENTRY: fd={client_fd} caller_pid={caller_pid} buf_len={}", buf.len());
        let req = if buf.len() >= std::mem::size_of::<NewProcessRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const NewProcessRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };

        // Take the socket fd sent via SCM_RIGHTS (from process-wide pool)
        let socket_fd = self.take_inflight_fd(client_fd as RawFd, req.socket_fd);

        let parent_pid = self.client_pid(client_fd as RawFd);
        let pid = self.state.create_process();
        if let Some(process) = self.state.processes.get_mut(&pid) {
            process.parent_pid = parent_pid;
        }
        self.process_init_count += 1;

        // Extract VARARG startup info.
        // VARARG layout: [object_attributes] [handles] [jobs] [startup_info] [env]
        // object_attributes: struct { rootdir:u32, attributes:u32, sd_len:u32, name_len:u32 }
        //   followed by sd_len bytes of SD + name_len bytes of name, aligned to 4.
        // We need to skip objattr + handles + jobs to get startup_info + env.
        let objattr_size = if buf.len() >= VARARG_OFF + 16 {
            let sd_len = u32::from_le_bytes([
                buf[VARARG_OFF + 8], buf[VARARG_OFF + 9],
                buf[VARARG_OFF + 10], buf[VARARG_OFF + 11],
            ]) as usize;
            let name_len = u32::from_le_bytes([
                buf[VARARG_OFF + 12], buf[VARARG_OFF + 13],
                buf[VARARG_OFF + 14], buf[VARARG_OFF + 15],
            ]) as usize;
            // Wine formula: (sizeof(objattr) + (sd_len & ~1) + (name_len & ~1) + 3) & ~3
            (16 + (sd_len & !1) + (name_len & !1) + 3) & !3
        } else {
            0
        };
        // Parse the handles array: parent-side handles to inherit into child.
        // VARARG layout: [objattr] [handles (u32 each)] [jobs] [startup_info] [env]
        let handles_start = VARARG_OFF + objattr_size;
        let handles_end = handles_start + req.handles_size as usize;
        let inherit_handles: Vec<u32> = if req.flags & 0x4 != 0 && req.handles_size >= 4 && handles_end <= buf.len() {
            // PROCESS_CREATE_FLAGS_INHERIT_HANDLES = 0x4
            buf[handles_start..handles_end]
                .chunks_exact(4)
                .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                .filter(|&h| h != 0)
                .collect()
        } else {
            Vec::new()
        };

        let vararg_start = handles_end + req.jobs_size as usize;

        // Split startup_info (info_size bytes) from environment (remaining bytes).
        // The VARARG layout packs [info (info_size bytes)][ROUND_SIZE padding][env].
        // Stock wineserver stores them separately and returns [info][env] (no padding)
        // in get_startup_info. We must do the same or Wine's init_startup_info
        // assertion fires: (char *)src == (char *)info + info_size.
        let info_size_clamped = req.info_size as usize;
        let (startup_info, startup_env) = if vararg_start < buf.len() {
            let remaining = &buf[vararg_start..];
            let actual_info_size = info_size_clamped.min(remaining.len());
            let mut info_data = remaining[..actual_info_size].to_vec();

            // FIXUP_LEN: clamp each string field so they can't exceed info_size.
            // Stock wineserver does this in create_startup_info. Without it,
            // malformed field lengths cause the init_startup_info assertion to fire.
            if info_data.len() >= 96 {
                let struct_hdr = 96usize;
                let mut budget = actual_info_size.saturating_sub(struct_hdr);
                // Fields at offsets 64..96: curdir, dllpath, imagepath, cmdline,
                //                          title, desktop, shellinfo, runtime
                for off in (64..96).step_by(4) {
                    let val = u32::from_le_bytes([
                        info_data[off], info_data[off+1], info_data[off+2], info_data[off+3],
                    ]) as usize;
                    let clamped = val.min(budget);
                    if clamped != val {
                        let bytes = (clamped as u32).to_le_bytes();
                        info_data[off..off+4].copy_from_slice(&bytes);
                    }
                    budget = budget.saturating_sub(clamped);
                }
            }

            // Environment starts after ROUND_SIZE(info_size) in the request VARARG
            let round_info = (info_size_clamped + 3) & !3;
            let env_start = round_info.min(remaining.len());
            let env_data = if env_start < remaining.len() {
                remaining[env_start..].to_vec()
            } else {
                Vec::new()
            };

            (Some(info_data), Some(env_data))
        } else {
            (None, None)
        };

        // Extract hstdin/hstdout/hstderr from startup_info_data
        // Layout: debug_flags(4) console_flags(4) console(4) hstdin(4) hstdout(4) hstderr(4)
        let (hstdin, hstdout, hstderr) = if let Some(ref si) = startup_info {
            if si.len() >= 24 {
                (
                    u32::from_le_bytes([si[12], si[13], si[14], si[15]]),
                    u32::from_le_bytes([si[16], si[17], si[18], si[19]]),
                    u32::from_le_bytes([si[20], si[21], si[22], si[23]]),
                )
            } else {
                (0, 0, 0)
            }
        } else {
            (0, 0, 0)
        };

        if let Some(process) = self.state.processes.get_mut(&pid) {
            process.startup_info = startup_info;
            process.startup_env = startup_env;
            process.info_size = info_size_clamped as u32;
            process.machine = req.machine;
            process.socket_fd = socket_fd;
        }

        // Handle inheritance: duplicate parent handles into child's table.
        // Wine's CreateProcess passes a list of inheritable handles. The child
        // expects these to exist at the same handle values. Without this,
        // services.exe -> rpcss.exe pipe communication fails (error 1726).
        //
        // Also duplicate hstdin/hstdout/hstderr from startup_info -- these are
        // the anonymous pipes for child process stdio.
        {
            // Collect all handles to inherit: explicit list + stdio handles
            let mut to_inherit: Vec<u32> = inherit_handles.clone();
            for &h in &[hstdin, hstdout, hstderr] {
                if h != 0 && !to_inherit.contains(&h) {
                    to_inherit.push(h);
                }
            }

            // Dup each handle from parent into child at the same handle value
            if !to_inherit.is_empty() {
                // Gather entries from parent first (avoid borrow conflict)
                let entries: Vec<(u32, crate::objects::HandleEntry)> = to_inherit.iter()
                    .filter_map(|&h| {
                        self.state.processes.get(&parent_pid)
                            .and_then(|p| p.handles.get(h))
                            .map(|e| {
                                let new_fd = e.fd.map(|f| unsafe { libc::dup(f) });
                                (h, crate::objects::HandleEntry {
                                    object_id: e.object_id,
                                    fd: new_fd,
                                    obj_type: e.obj_type,
                                    access: e.access,
                                    options: e.options,
                                })
                            })
                    })
                    .collect();

                let count = entries.len();
                if let Some(child) = self.state.processes.get_mut(&pid) {
                    for (handle_val, entry) in entries {
                        child.handles.insert_at(handle_val, entry);
                    }
                }
                if count > 0 {
                    log_info!("new_process: inherited {count} handles from pid={parent_pid} into pid={pid}");
                }
            }
        }

        // Set up the child's connection via the socketpair fd.
        // Wine's parent sends one end of a socketpair to us (socket_fd) and
        // passes the other end to the child as WINESERVERSOCKET. We perform
        // the same handshake as accept(): create request pipe, send write end
        // to child via SCM_RIGHTS on the socketpair.
        if let Some(sock_fd) = socket_fd {
            if let Some((client, msg_fd)) = crate::ipc::setup_client_on_socket(sock_fd) {
                let request_fd = client.fd;
                epoll_add(self.epoll_fd, request_fd, libc::EPOLLIN as u32);
                epoll_add(self.epoll_fd, msg_fd, libc::EPOLLIN as u32);
                self.msg_fd_map.insert(msg_fd, request_fd);
                // Pre-assign this client to the new process
                let mut c = client;
                c.process_id = pid;
                self.clients.insert(request_fd, c);
                if self.clients.len() > self.peak_clients {
                    self.peak_clients = self.clients.len();
                }
            }
        }

        // Also queue for FIFO matching (fallback if child connects via master socket)
        self.state.unclaimed_pids.push_back(pid);

        // Allocate waitable handle in parent's handle table (process handles are waitable)
        let parent_pid = self.client_pid(client_fd as RawFd);
        let handle = self.alloc_waitable_handle_for_client(client_fd);
        // Store child's wine PID in the process handle's object_id.
        // new_thread resolves target process by looking up the process handle's object_id.
        if handle != 0 {
            if let Some(process) = self.state.processes.get_mut(&parent_pid) {
                if let Some(entry) = process.handles.get_mut(handle) {
                    entry.object_id = pid as u64;
                }
            }
        }
        // Process handle: signaled when child exits.
        // Arc clone keeps the fd alive even if parent closes its handle.
        if handle != 0 {
            if let Some(obj) = self.get_or_create_event(true, false) {
                let exit_clone = Arc::clone(&obj);
                self.insert_recyclable_event(parent_pid, handle, obj, 1); // INTERNAL
                self.process_exit_events.entry(pid).or_default().push((parent_pid, handle, exit_clone));
            }
        }

        // Idle event: manual-reset, initially unsignaled. Signaled when the child
        // first enters a blocking wait (its message loop). Returned by
        // get_process_idle_event so the parent can WaitForInputIdle via ntsync.
        if let Some(obj) = self.get_or_create_event(true, false) {
            self.process_idle_events.insert(pid, obj);
        }

        // Info handle: waitable handle that the parent NtWaitForSingleObject's on.
        // Signaled when child calls init_process_done.
        // We dup the ntsync object so it survives close_handle — the parent often
        // closes the info handle before the child calls init_process_done.
        let info = self.alloc_waitable_handle_for_client(client_fd);
        if info != 0 {
            if let Some(obj) = self.get_or_create_event(true, false) {
                let info_dup = obj.dup();
                self.insert_recyclable_event(parent_pid, info, obj, 1); // INTERNAL
                // Store dup'd object keyed by (parent_pid, info) so init_process_done
                // can signal it even after close_handle removed the primary.
                if let Some(dup_obj) = info_dup {
                    // Use a separate ntsync_objects entry with a synthetic key that
                    // won't collide: store on the CHILD pid instead of parent.
                    // init_process_done iterates process_info_handles to find
                    // (parent_pid, handle) — we need to keep the primary key.
                    // Instead: don't use recyclable for info handles so close_handle
                    // skips them. Mark them non-recyclable.
                    // Actually, simplest: just keep the dup alive in process_info_handles.
                    self.state.process_info_handles.insert(info, crate::objects::ProcessInfoHandle {
                        target_pid: pid,
                        parent_pid,
                        ntsync_obj_fd: dup_obj.fd(),
                    });
                    std::mem::forget(dup_obj); // fd owned by process_info_handles now
                } else {
                    self.state.process_info_handles.insert(info, crate::objects::ProcessInfoHandle {
                        target_pid: pid,
                        parent_pid,
                        ntsync_obj_fd: -1,
                    });
                }
            } else {
                self.state.process_info_handles.insert(info, crate::objects::ProcessInfoHandle {
                    target_pid: pid,
                    parent_pid,
                    ntsync_obj_fd: -1,
                });
            }
        }


        let reply = NewProcessReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            info,
            pid,
            handle,
            _pad_0: [0; 4],
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_get_new_process_info(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let req = if buf.len() >= std::mem::size_of::<GetNewProcessInfoRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const GetNewProcessInfoRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };

        // Wine's NtCreateUserProcess checks success before returning to the caller.
        // In the real wineserver, the parent's Select blocks until the child calls
        // init_process_done. Return success=1 if the child process exists and has
        // living threads (still running). If it died (no threads left), return
        // success=0 so the parent knows the child failed.
        let (success, exit_code) = self.state.process_info_handles.get(&req.info)
            .and_then(|h| self.state.processes.get(&h.target_pid))
            .map(|p| {
                if p.threads.is_empty() && p.startup_done {
                    // Child exited — could be normal exit or early death
                    (0i32, p.exit_code)
                } else {
                    // Child still running (or hasn't connected yet)
                    (1i32, p.exit_code)
                }
            })
            .unwrap_or((0, 0));

        let reply = GetNewProcessInfoReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            success,
            exit_code,
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_init_process_done(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let req = if buf.len() >= std::mem::size_of::<InitProcessDoneRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const InitProcessDoneRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };

        let pid = self.clients.get(&(client_fd as RawFd))
            .and_then(|c| if c.process_id != 0 { Some(c.process_id) } else { None });

        if let Some(process) = pid.and_then(|p| self.state.processes.get_mut(&p)) {
            process.peb = req.peb;
            process.startup_done = true;
        }

        // Signal all process_info handles targeting this pid.
        // This wakes the parent's NtWaitForSingleObject(process_info).
        // Use the dup'd ntsync fd from ProcessInfoHandle — it survives
        // close_handle on the parent's copy of the info handle.
        if let Some(pid) = pid {
            let info_entries: Vec<(u32, u32, i32)> = self.state.process_info_handles.iter()
                .filter(|(_, v)| v.target_pid == pid)
                .map(|(&handle, v)| (v.parent_pid, handle, v.ntsync_obj_fd))
                .collect();
            // Collect what to wake, then wake — avoids borrow conflicts
            let mut to_wake: Vec<(u32, u32)> = Vec::new();
            for (parent_pid, ih, dup_fd) in &info_entries {
                if let Some((obj, _)) = self.ntsync_objects.get(&(*parent_pid, *ih)) {
                    let result = obj.event_set();
                } else if *dup_fd >= 0 {
                    let obj = crate::ntsync::NtsyncObj::from_raw_fd(unsafe { libc::dup(*dup_fd) });
                    let result = obj.event_set();
                } else {
                    log_error!("init_process_done: info handle {ih:#x} (parent_pid={parent_pid}) — no ntsync object and no dup fd!");
                }
                to_wake.push((*parent_pid, *ih));
            }
            // Wake fsync slots AFTER releasing ntsync_objects borrow
            for (ppid, handle) in to_wake {
                self.fsync_wake_handle(ppid, handle);
            }
        }

        // DO NOT signal idle event here. The correct trigger is in handle_select
        // (first blocking wait = process entered its message loop). Signaling early
        // at init_process_done causes a race: the game thread's WaitForInputIdle
        // returns before explorer finishes display driver init, both threads hit
        // update_display_cache simultaneously → user_check_not_lock assertion.
        // With winex11.drv, explorer WILL reach handle_select when it enters its
        // X11 event loop. The 10-second timeout in NtUserWaitForInputIdle is the
        // safety net if it somehow doesn't.

        // Stock wineserver returns suspend=1 for child processes (spawned via new_process).
        // The parent must call resume_thread to wake the child. Only the initial process
        // (parent_pid=0, the one that started the server) gets suspend=0.
        let suspend = if let Some(process) = pid.and_then(|p| self.state.processes.get(&p)) {
            if process.parent_pid != 0 { 1i32 } else { 0i32 }
        } else {
            0i32
        };

        log_info!("init_process_done: pid={:?} peb=0x{:x} suspend={suspend} → error=0", pid, req.peb);

        let reply = InitProcessDoneReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            suspend,
            _pad_0: [0; 4],
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_get_process_info(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let pid = self.clients.get(&(client_fd as RawFd))
            .and_then(|c| if c.process_id != 0 { Some(c.process_id) } else { None })
            .unwrap_or(0);

        let max_vararg = max_reply_vararg(buf);

        // Get exe pe_image_info if available
        let pe_info = self.state.processes.get(&pid)
            .and_then(|p| p.exe_image_info.as_ref());

        if let Some(pe_info) = pe_info {
            let vararg_len = pe_info.len().min(max_vararg as usize);
            let reply = GetProcessInfoReply {
                header: ReplyHeader { error: 0, reply_size: vararg_len as u32 },
                pid,
                ppid: 0,
                affinity: u64::MAX,
                peb: self.state.processes.get(&pid).map(|p| p.peb).unwrap_or(0),
                start_time: 0,
                end_time: 0,
                session_id: 0,
                exit_code: self.state.processes.get(&pid).map(|p| p.exit_code).unwrap_or(0),
                priority: 8,
                base_priority: 8,
                disable_boost: 0,
                machine: 0x8664,
            };
            return reply_vararg(&reply, &pe_info[..vararg_len]);
        }

        let reply = GetProcessInfoReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            pid,
            ppid: 0,
            affinity: u64::MAX,
            peb: 0,
            start_time: 0,
            end_time: 0,
            session_id: 0,
            exit_code: 0,
            priority: 8,
            base_priority: 8,
            disable_boost: 0,
            machine: 0x8664,
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_terminate_process(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let req = if buf.len() >= std::mem::size_of::<TerminateProcessRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const TerminateProcessRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };
        let pid = self.clients.get(&(client_fd as RawFd))
            .and_then(|c| if c.process_id != 0 { Some(c.process_id) } else { None });
        let tid = self.clients.get(&(client_fd as RawFd))
            .map(|c| c.thread_id).unwrap_or(0);
        let is_self = if req.handle == 0 || req.handle == 0xFFFFFFFF {
            // Current process — set exit code and remove all other threads.
            // Threads created via NewThread but never connected (no init_thread)
            // still sit in the thread list. If we don't remove them, the last
            // real thread's disconnect won't fire the process exit signal.
            if let Some(pid) = pid {
                if let Some(process) = self.state.processes.get_mut(&pid) {
                    process.exit_code = req.exit_code;
                    let before = process.threads.len();
                    process.threads.retain(|&t| t == tid);
                    if before != process.threads.len() {
                    }
                }
            }
            1
        } else {
            // Terminating another process by handle
            0
        };
        let reply = TerminateProcessReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            is_self,
            _pad_0: [0; 4],
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_get_process_debug_info(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        let reply = GetProcessDebugInfoReply {
            header: ReplyHeader { error: 0xC0000353, reply_size: 0 }, // STATUS_PORT_NOT_SET — no debugger
            debug: 0,
            debug_children: 0,
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_grant_process_admin_token(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        // Stock always returns success. No real privilege check needed.
        reply_fixed(&ReplyHeader { error: 0, reply_size: 0 })
    }


    pub(crate) fn handle_make_process_system(&mut self, client_fd: i32, _buf: &[u8]) -> Reply {
        let handle = self.alloc_waitable_handle_for_client(client_fd);
        let pid = self.client_pid(client_fd as RawFd);

        // Create or reuse the global shutdown_event (manual-reset, unsignaled).
        // All system processes share the same ntsync event.
        // When all user (non-system) processes exit, we signal it.
        if handle != 0 {
            if self.shutdown_event.is_none() {
                if let Some(obj) = self.get_or_create_event(true, false) {
                    self.shutdown_event = Some(obj);
                }
            }
            // Clone the shutdown_event Arc for this process's handle
            if let Some(ref evt) = self.shutdown_event {
                self.ntsync_objects.insert((pid, handle), (Arc::clone(evt), 1)); // INTERNAL
            }
            self.system_pids.insert(pid);
        }

        let reply = MakeProcessSystemReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            event: handle,
            _pad_0: [0; 4],
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_open_process(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let _req = if buf.len() >= std::mem::size_of::<OpenProcessRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const OpenProcessRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };
        let handle = self.alloc_waitable_handle_for_client(client_fd);
        if handle == 0 {
            return reply_fixed(&ReplyHeader { error: 0xC0000017, reply_size: 0 });
        }
        let pid = self.client_pid(client_fd as RawFd);
        if let Some(obj) = self.get_or_create_event(true, false) {
            self.ntsync_objects.insert((pid, handle), (obj, 1)); // INTERNAL
        }
        let reply = OpenProcessReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            handle,
            _pad_0: [0; 4],
        };
        reply_fixed(&reply)
    }


    pub(crate) fn handle_set_process_info(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        reply_fixed(&SetProcessInfoReply { header: ReplyHeader { error: 0, reply_size: 0 } })
    }


    // Process memory
    pub(crate) fn handle_read_process_memory(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        reply_fixed(&ReplyHeader { error: 0, reply_size: 0 })
    }

    pub(crate) fn handle_write_process_memory(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        reply_fixed(&WriteProcessMemoryReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            written: 0,
            _pad_0: [0; 4],
        })
    }


    pub(crate) fn handle_list_processes(&mut self, _client_fd: i32, _buf: &[u8]) -> Reply {
        reply_fixed(&ListProcessesReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            info_size: 0,
            process_count: 0,
            total_thread_count: 0,
            total_name_len: 0,
        })
    }


    // Process idle event: returns a waitable handle that signals when the target
    // process first enters a blocking wait (its message loop). Used by WaitForInputIdle.
    pub(crate) fn handle_get_process_idle_event(&mut self, client_fd: i32, buf: &[u8]) -> Reply {
        let req = if buf.len() >= std::mem::size_of::<GetProcessIdleEventRequest>() {
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const GetProcessIdleEventRequest) }
        } else {
            return reply_fixed(&ReplyHeader { error: 0xC000000D, reply_size: 0 });
        };

        let caller_pid = self.client_pid(client_fd as RawFd);

        // req.handle is a process handle in the caller's table. object_id = target pid.
        let target_pid = self.state.processes.get(&caller_pid)
            .and_then(|p| p.handles.get(req.handle))
            .map(|entry| entry.object_id as u32)
            .unwrap_or(0);

        // Look up the target's idle event ntsync object
        let idle_fd = self.process_idle_events.get(&target_pid).map(|obj| obj.fd());

        let event_handle = if let Some(src_fd) = idle_fd {
            // Dup the ntsync fd so the caller gets their own reference to the kernel object
            let dup_fd = unsafe { libc::dup(src_fd) };
            if dup_fd >= 0 {
                let handle = self.alloc_waitable_handle_for_client(client_fd);
                if handle != 0 {
                    let obj = Arc::new(crate::ntsync::NtsyncObj::from_raw_fd(dup_fd));
                    self.ntsync_objects.insert((caller_pid, handle), (obj, 1)); // INTERNAL
                    handle
                } else {
                    unsafe { libc::close(dup_fd); }
                    0
                }
            } else { 0 }
        } else {
            0
        };

        reply_fixed(&GetProcessIdleEventReply {
            header: ReplyHeader { error: 0, reply_size: 0 },
            event: event_handle,
            _pad_0: [0; 4],
        })
    }
}
