// Client connection lifecycle — accept, message handling, disconnect

use super::*;

#[allow(unused_variables)]
impl EventLoop {


    /// Clean up thread/process state for a stale disconnect (fd was reused).
    /// Does everything disconnect_client does EXCEPT touching ev.clients —
    /// the phantom at this fd now belongs to a different process.
    pub(crate) fn cleanup_stale_thread(&mut self, pid: u32, tid: u32, old_fd: RawFd) {

        // Free SHM thread queue slot
        if tid != 0 {
            self.shm.free_slot(tid);
        }

        // Remove thread from process thread list
        if pid != 0 && tid != 0 {
            if let Some(process) = self.state.processes.get_mut(&pid) {
                process.threads.retain(|&t| t != tid);
            }
        }

        // Signal thread exit events (keyed by old fd)
        let mut stale_wakes: Vec<(u32, u32)> = Vec::new();
        if let Some(entries) = self.thread_exit_events.remove(&old_fd) {
            for (_creator_pid, _handle, obj) in &entries {
                let _ = obj.event_set();
                stale_wakes.push((*_creator_pid, *_handle));
            }
        }
        for (cpid, h) in stale_wakes {
            self.fsync_wake_handle(cpid, h);
        }

        // Check if this was the LAST thread of the process
        let remaining_threads = if pid != 0 {
            self.state.processes.get(&pid).map(|p| p.threads.len()).unwrap_or(0)
        } else { 0 };

        if pid != 0 && tid != 0 && remaining_threads == 0 {
            // Last thread of old process — close its msg_fd.
            // Find it via msg_fd_map (reverse lookup: find msg_fd that mapped to a fd for this process).
            let old_msg_fd = self.msg_fd_map.iter()
                .find(|(_, req_fd)| {
                    self.clients.get(req_fd).map_or(false, |c| c.process_id == pid)
                })
                .map(|(&mfd, _)| mfd)
                .or_else(|| {
                    // Fallback: check process state for socket_fd
                    self.state.processes.get(&pid).and_then(|p| p.socket_fd)
                });
            if let Some(mfd) = old_msg_fd {
                unsafe { libc::close(mfd); }
                self.msg_fd_map.remove(&mfd);
            }

            // Clean up process-wide inflight fd pool
            if let Some(pool) = self.process_inflight_fds.remove(&pid) {
                for (_, _, fd) in pool {
                    unsafe { libc::close(fd); }
                }
            }

            let did_init = self.state.processes.get(&pid)
                .map(|p| p.startup_done).unwrap_or(false);

            if let Some(process) = self.state.processes.get_mut(&pid) {
                process.exit_code = 0;
                process.startup_done = true;
            }

            if !did_init {
                log_warn!("EARLY_DEATH (stale): pid={pid} died before init_process_done");
                let info_entries: Vec<(u32, u32)> = self.state.process_info_handles.iter()
                    .filter(|(_, v)| v.target_pid == pid)
                    .map(|(&handle, v)| (v.parent_pid, handle))
                    .collect();
                for (parent_pid, ih) in &info_entries {
                    if let Some((obj, _)) = self.ntsync_objects.get(&(*parent_pid, *ih)) {
                        let _ = obj.event_set();
                    }
                }
                for (parent_pid, ih) in &info_entries {
                    self.fsync_wake_handle(*parent_pid, *ih);
                }
            }

            if let Some(idle_event) = self.process_idle_events.remove(&pid) {
                let _ = idle_event.event_set();
            }

            let mut exit_wakes: Vec<(u32, u32)> = Vec::new();
            if let Some(entries) = self.process_exit_events.remove(&pid) {
                for (_parent_pid, _handle, obj) in &entries {
                    let _ = obj.event_set();
                    exit_wakes.push((*_parent_pid, *_handle));
                }
            }
            for (ppid, h) in exit_wakes {
                self.fsync_wake_handle(ppid, h);
            }
            self.fsync_wake_all_for_pid(pid);

            if !self.system_pids.contains(&pid) {
                let user_processes_alive = self.state.processes.iter()
                    .filter(|(ppid, p)| !self.system_pids.contains(ppid) && !p.threads.is_empty())
                    .count();
                if user_processes_alive == 0 {
                    if let Some(ref evt) = self.shutdown_event {
                        let _ = evt.event_set();
                        log_info!("stale_cleanup: ALL user processes gone, signaled shutdown_event!");
                    }
                    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                    self.linger_deadline = Some(deadline);
                    log_info!("stale_cleanup: linger started (5s deadline)");
                }
            }
        }
    }

    pub(crate) fn disconnect_client(&mut self, fd: RawFd) {
        // fd is the request_fd (pipe read end)
        epoll_del(self.epoll_fd, fd);
        let (pid, tid, msg_fd) = if let Some(client) = self.clients.get(&fd) {
            let pid = client.process_id;
            let tid = client.thread_id;
            let msg_fd = client.msg_fd;
            (pid, tid, msg_fd)
        } else {
            (0, 0, -1)
        };
        // Clean up per-client state. Stock wineserver's kill_thread does NOT
        // signal alerts or queue APCs for dying/sibling threads — it just abandons
        // mutexes and signals the thread handle. Alert signals for system APCs
        // would trigger Wine's sync.c:441 assertion in the inproc ntsync path.
        self.client_alerts.remove(&fd);
        self.client_worker_interrupts.remove(&fd);
        self.client_apc_flags.remove(&fd);

        // Abandon mutexes owned by this thread. Stock wineserver calls
        // abandon_mutexes(thread) → NTSYNC_IOC_MUTEX_KILL for each owned mutex.
        // Without this, mutexes held by dying threads stay locked forever,
        // hanging any process that tries to acquire them.
        if tid != 0 {
            let mutex_keys: Vec<(u32, u32)> = self.ntsync_objects.keys()
                .filter(|(p, _)| *p == pid)
                .copied()
                .collect();
            for (p, h) in mutex_keys {
                if let Some((obj, sync_type)) = self.ntsync_objects.get(&(p, h)) {
                    // sync_type 2 = mutex (from our create_mutex handler)
                    if *sync_type == 2 {
                        let _ = obj.mutex_kill(tid);
                    }
                }
            }
        }

        self.clients.remove(&fd); // Drop closes all fds
        // Pending waits for this fd are cleaned up lazily in check_pending_waits()

        // Free SHM thread queue slot so it can be reused by future threads
        if tid != 0 {
            self.shm.free_slot(tid);
        }

        // Clean up pending PIPE_WAIT waiters for this process
        if pid != 0 {
            for waiters in self.pending_pipe_waiters.values_mut() {
                waiters.retain(|w: &super::pipes::PendingPipeWaiter| w.pid != pid);
            }
            self.pending_pipe_waiters.retain(|_: &String, v: &mut Vec<super::pipes::PendingPipeWaiter>| !v.is_empty());
        }

        // Remove thread from process thread list
        if pid != 0 && tid != 0 {
            if let Some(process) = self.state.processes.get_mut(&pid) {
                process.threads.retain(|&t| t != tid);
                // Clean up ghost threads: created by new_thread but never connected.
                // Without this, EARLY_DEATH never fires (remaining_threads > 0).
                let connected_tids: Vec<u32> = self.clients.values()
                    .filter(|c| c.process_id == pid && c.thread_id != 0)
                    .map(|c| c.thread_id)
                    .collect();
                let before = process.threads.len();
                process.threads.retain(|t| connected_tids.contains(t));
                if before != process.threads.len() {
                }
            }
        }

        // Signal ntsync events for thread handles (WaitForSingleObject on thread).
        // thread_exit_events owns dup'd fds, so this works even if close_handle ran.
        if let Some(entries) = self.thread_exit_events.remove(&fd) {
            for (creator_pid, handle, obj) in &entries {
                let _ = obj.event_set();
                self.fsync_wake_handle(*creator_pid, *handle);
            }
        }

        // Check if this was the LAST thread of the process
        let remaining_threads = if pid != 0 {
            self.state.processes.get(&pid).map(|p| p.threads.len()).unwrap_or(0)
        } else { 0 };

        if pid != 0 && tid != 0 && remaining_threads == 0 {
            // Last thread died — process is truly dead.
            // Now safe to close msg_fd and remove from mapping.
            // msg_fd is shared between all threads — phantom Drop no longer
            // closes it, so we must do it here.
            if msg_fd >= 0 {
                unsafe { libc::close(msg_fd); }
            }
            epoll_del(self.epoll_fd, msg_fd);
            self.msg_fd_map.remove(&msg_fd);

            // Clean up process-wide inflight fd pool
            if let Some(pool) = self.process_inflight_fds.remove(&pid) {
                for (_, _, fd) in pool {
                    unsafe { libc::close(fd); }
                }
            }

            let did_init = self.state.processes.get(&pid)
                .map(|p| p.startup_done).unwrap_or(false);

            if let Some(process) = self.state.processes.get_mut(&pid) {
                process.exit_code = 0;
                process.startup_done = true;
            }

            // Phase 1a: If child died before init_process_done, signal info
            // handles so the parent's Select on the info handle wakes up.
            // Without this, the parent hangs forever on INFINITE timeout.
            if !did_init {
                log_warn!("EARLY_DEATH: pid={pid} died before init_process_done");
                let info_entries: Vec<(u32, u32)> = self.state.process_info_handles.iter()
                    .filter(|(_, v)| v.target_pid == pid)
                    .map(|(&handle, v)| (v.parent_pid, handle))
                    .collect();
                for (parent_pid, ih) in &info_entries {
                    if let Some((obj, _)) = self.ntsync_objects.get(&(*parent_pid, *ih)) {
                        let result = obj.event_set();
                        log_warn!("EARLY_DEATH: signaled info handle {ih:#x} (parent_pid={parent_pid}) for dead pid={pid} result={result:?}");
                    }
                }
                // Wake fsync slots after releasing ntsync_objects borrow
                for (parent_pid, ih) in &info_entries {
                    self.fsync_wake_handle(*parent_pid, *ih);
                }
            }

            // NOTE: previously drained ALL named_sync on any early death.
            // This was wrong — it destroyed __wineboot_event and __wine_svcctlstarted
            // when an unrelated WoW64 helper died, breaking event sharing between
            // wineboot and services.exe. Named sync objects are global and must
            // persist across process lifetimes.

            // Signal + clean up idle event so WaitForInputIdle waiters don't hang
            if let Some(idle_event) = self.process_idle_events.remove(&pid) {
                let _ = idle_event.event_set(); // signal before drop
            }

            // Signal ntsync events for process handles held by parents.
            // process_exit_events owns dup'd NtsyncObj fds, so this works even
            // if the parent already closed its handle via close_handle.
            if let Some(entries) = self.process_exit_events.remove(&pid) {
                for (parent_pid, handle, obj) in &entries {
                    let result = obj.event_set();
                    // Also signal fsync shm for this handle so client-side futex waiters wake
                    self.fsync_wake_handle(*parent_pid, *handle);
                }
            }

            // Job notifications: post JOB_OBJECT_MSG_EXIT_PROCESS to completion port
            if let Some(job_oid) = self.process_job.remove(&pid) {
                let completion_info = self.jobs.get(&job_oid)
                    .and_then(|j| j.completion_port_handle.map(|port| (port, j.completion_key)));
                if let Some(job) = self.jobs.get_mut(&job_oid) {
                    job.processes.retain(|&p| p != pid);
                    job.num_processes = job.num_processes.saturating_sub(1);
                }
                if let Some((port, ckey)) = completion_info {
                    // JOB_OBJECT_MSG_EXIT_PROCESS = 4
                    self.completion_queues.entry(port).or_default().push(CompletionMsg {
                        ckey, cvalue: pid as u64, information: 0, status: 4,
                    });
                    let zero = self.jobs.get(&job_oid).map(|j| j.num_processes == 0).unwrap_or(false);
                    if zero {
                        // JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 8
                        self.completion_queues.entry(port).or_default().push(CompletionMsg {
                            ckey, cvalue: 0, information: 0, status: 8,
                        });
                    }
                    // Wake any thread waiting on the completion port
                    if let Some(waiters) = self.completion_waiters.get_mut(&port) {
                        while let Some(waiter) = waiters.pop() {
                            if let Some(queue) = self.completion_queues.get_mut(&port) {
                                if !queue.is_empty() {
                                    let msg = queue.remove(0);
                                    self.thread_completion_cache.insert(waiter.client_fd, msg);
                                    if let Some((obj, _)) = self.ntsync_objects.get(&(waiter.pid, waiter.wait_handle)) {
                                        let _ = obj.event_set();
                                    }
                                }
                            }
                        }
                        if waiters.is_empty() {
                            self.completion_waiters.remove(&port);
                        }
                    }
                }
            }

            // Wake ALL fsync slots for the dying process so any waiters unblock
            self.fsync_wake_all_for_pid(pid);

            // Check if this was a user (non-system) process dying.
            // If no user processes remain, signal shutdown_event to wake system processes
            // and start the linger timer — don't exit yet, new processes may connect
            // (e.g., game launching after wineboot finishes).
            if !self.system_pids.contains(&pid) {
                let user_processes_alive = self.state.processes.iter()
                    .filter(|(ppid, p)| !self.system_pids.contains(ppid) && !p.threads.is_empty())
                    .count();
                if user_processes_alive == 0 {
                    if let Some(ref evt) = self.shutdown_event {
                        let _ = evt.event_set();
                        log_info!("disconnect: ALL user processes gone, signaled shutdown_event!");
                    }
                    // Save registry NOW — the process might be killed during linger.
                    self.registry.save_to_prefix(&self.user_sid_str);

                    // Start linger: wait 5s for new connections before exiting.
                    // This bridges the gap between wineboot exit and game connect.
                    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                    self.linger_deadline = Some(deadline);
                    log_info!("disconnect: linger started (5s deadline)");
                }
            }
        } else if pid != 0 && tid != 0 {
        } else if pid != 0 && tid == 0 {
        }
    }
}
