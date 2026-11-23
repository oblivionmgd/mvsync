"""MVSync TouchDesigner extension (spec §6). Lives on the mvsync COMP as ext MVSync.

TD main thread only does: OSC receive, display, play clock. No waiting on subprocesses,
no network, no extra libraries (numpy only, not even that here).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

STATES = ["IDLE", "SEARCHING", "LOCKED", "HOLD", "LOST", "IDENTIFYING_ONLINE", "FETCHING", "NO_MV", "PROVISIONAL", "REMIX"]
# what the online lookup status means, for the Online field on the Status page
ONLINE_LABELS = {"no_mv": "MVなし", "no_candidates": "MVなし", "no_match": "照合で不一致", "recognised": "曲名判明",
                 "verifying": "確認中", "adopted": "採用", "in_library": "ライブラリにあり", "not_recognised": "曲名不明",
                 "timeout": "Shazam 応答なし", "recognizer_error": "Shazam エラー", "cancelled": "取り消し",
                 "remix": "Remix・原曲を検索", "remix_adopted": "Remix・原曲MV", "remix_playing": "Remix 継続中"}
IS_WIN = sys.platform.startswith("win")


def _words(text):
    """'a, b、c' -> ['a', 'b', 'c'] (same rule as worker/title_words.parse)."""
    out = []
    for w in re.split(r"[,、，\n]", text or ""):
        w = re.sub(r"\s+", " ", w).strip().lower()
        if w and w not in out:
            out.append(w)
    return out


def _x(p):
    return os.path.expanduser(os.path.expandvars(p or ""))


class MVSyncExt:
    def __init__(self, ownerComp):
        self.owner = ownerComp
        self.slots = {"A": self._empty_slot(), "B": self._empty_slot()}
        self.active = "A"
        self.pending = None           # slot being prepared for a track change
        self.fade = None              # (from_slot, to_slot, t_start)
        self.est = None               # dict(id,pos,rate,conf,t_ref)
        self.state = "IDLE"
        self.clk_off = 0.0            # worker clock - TD clock
        # kept in COMP storage so a re-created extension does not think the worker died
        self.last_hb = float(self.owner.fetch("last_hb", 0.0, search=False))
        self.last_tick = time.time()
        self.track_changed_frame = False
        self.titles = {}
        self.bpm_base = {}
        self.proc = None
        self.update_proc = None
        self.install_proc = None
        self.install_poll = 0.0
        self.restarts = 0
        self.nudge_ms = 0.0
        self.library = {}
        self.silent = False
        self.owner.store("ext_loaded", time.time())
        try:
            self._set_install_status()
        except Exception:
            pass
        try:
            self.Send("/mvsync/resend")
        except Exception:
            pass

    # ================================================================== helpers
    @staticmethod
    def _empty_slot():
        return {"id": "", "src": "", "ph": 0.0, "rate": 1.0, "opened": 0.0, "dur": 0.0}

    def _op(self, name):
        return self.owner.op(name)

    def Log(self, msg):
        msg = str(msg)
        line = time.strftime("%H:%M:%S ") + msg
        d = self._op("log")
        if d is not None:
            d.appendRow([line])
            while d.numRows > 300:
                d.deleteRow(0)
        self.owner.par.Lastlog = msg.splitlines()[0][:200] if msg else ""

    def Send(self, address, *args):
        o = self._op("oscout")
        if o is not None:
            o.sendOSC(address, list(args))

    def config_path(self):
        return os.path.join(_x(self.owner.par.Libraryfolder.eval()), "config.json")

    # ---- paths: an empty Config field means "find it automatically"
    def app_dir(self):
        """Where Install unpacks the worker that is embedded in the tox."""
        return os.path.join(_x(self.owner.par.Libraryfolder.eval()), "app")

    def payload(self):
        """Embedded worker files (release tox only): {relative path: Text DAT}."""
        idx = self._op("payload/files")
        if idx is None:
            return {}
        out = {}
        for r in range(1, idx.numRows):
            d = self._op("payload/" + idx[r, "dat"].val)
            if d is not None:
                out[idx[r, "path"].val] = d
        return out

    def payload_version(self):
        idx = self._op("payload/files")
        cell = idx[0, 2] if idx is not None and idx.numCols > 2 else None
        return cell.val.split("version=", 1)[-1] if cell is not None and "version=" in cell.val else ""

    def code_dir(self):
        """Folder holding worker/. Empty Codedir -> the installed app (<Library>/app), next to the tox,
        then the project folder."""
        given = _x(self.owner.par.Codedir.eval())
        cands = [given] if given else [self.app_dir()]
        try:
            tox = _x(self.owner.par.externaltox.eval())
            if tox:
                if not os.path.isabs(tox):
                    tox = os.path.join(project.folder, tox)
                cands.append(os.path.dirname(os.path.abspath(tox)))
        except Exception:
            pass
        cands += [project.folder, os.path.join(project.folder, "mvsync")]
        for d in cands:
            if d and os.path.exists(os.path.join(d, "worker", "mvsync_worker.py")):
                return d
        return given or project.folder

    def python_path(self):
        given = _x(self.owner.par.Pythonpath.eval())
        if given:
            return given
        rel = ("Scripts", "python.exe") if IS_WIN else ("bin", "python")
        return os.path.join(self.code_dir(), ".venv", *rel)

    def ytdlp_path(self):
        given = _x(self.owner.par.Ytdlppath.eval())
        if given:
            return given
        cand = os.path.join(os.path.dirname(self.python_path()), "yt-dlp.exe" if IS_WIN else "yt-dlp")
        if os.path.exists(cand):
            return cand
        return shutil.which("yt-dlp") or "yt-dlp"

    def node_dir(self):
        given = _x(self.owner.par.Nodedir.eval())
        if given:
            return given
        found = shutil.which("node")
        for d in ([os.path.dirname(found)] if found else []) + ["/opt/homebrew/bin", "/usr/local/bin"]:
            if os.path.exists(os.path.join(d, "node.exe" if IS_WIN else "node")):
                return d
        return ""

    # ================================================================== install (setup.sh / setup.bat)
    def _install_log(self):
        return os.path.join(_x(self.owner.par.Libraryfolder.eval()), "logs", "install.log")

    def _installed_version(self):
        try:
            with open(os.path.join(self.app_dir(), "VERSION"), encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""

    def _set_install_status(self, text=None):
        if not hasattr(self.owner.par, "Installstatus"):
            return
        if text is None:
            if not os.path.exists(self.worker_script()) and not self.payload():
                text = "Code folder を設定してください（worker/ が見つかりません）"
            elif not os.path.exists(self.python_path()):
                text = "未インストール（Install を押してください）"
            elif (self.payload() and not self.owner.par.Codedir.eval()
                  and self._installed_version() != self.payload_version()):
                text = "更新があります（Install を押してください）"
            else:
                text = "準備完了"
        self.owner.par.Installstatus = text

    def _unpack_payload(self):
        """Writes the embedded worker / setup files to <Library>/app (overwrites: Install = repair/update)."""
        root = self.app_dir()
        for rel, d in self.payload().items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            text = d.text
            if rel.endswith(".bat"):
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if rel.endswith(".sh") and not IS_WIN:
                os.chmod(path, 0o755)
        with open(os.path.join(root, "VERSION"), "w", encoding="utf-8") as f:
            f.write(self.payload_version())
        return root

    def Install(self):
        """Creates <code folder>/.venv with everything the worker needs, in the background."""
        if self.install_proc is not None and self.install_proc.poll() is None:
            self.Log("install already running")
            return
        if self._alive():
            self.Stop(kill_after=0.5)
        if self.payload() and not self.owner.par.Codedir.eval():
            try:
                root = self._unpack_payload()
            except Exception as e:
                self.Log("unpack failed: %s" % e)
                self._set_install_status("失敗: ファイルを書き出せません（%s）" % e)
                return
        else:
            root = self.code_dir()
        script = os.path.join(root, "setup.bat" if IS_WIN else "setup.sh")
        if not os.path.exists(script):
            self.Log("setup script not found: %s" % script)
            self._set_install_status("失敗: %s がありません" % os.path.basename(script))
            return
        log = self._install_log()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        out = open(log, "w", encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env["MVSYNC_NO_PAUSE"] = "1"
        if IS_WIN:
            cmd, kw = ["cmd", "/c", script], {"creationflags": 0x08000000}
        else:
            cmd, kw = ["/bin/sh", script], {"start_new_session": True}
        self.install_proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             cwd=root, env=env, **kw)
        self._set_install_status("インストール中…")
        self.Log("install started: %s (log: %s)" % (script, log))

    def _poll_install(self, now):
        if self.install_proc is None or now - self.install_poll < 0.5:
            return
        self.install_poll = now
        code = self.install_proc.poll()
        last = ""
        try:
            with open(self._install_log(), encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            last = lines[-1] if lines else ""
        except Exception:
            pass
        if code is None:
            self._set_install_status("インストール中… " + last[:80])
            return
        self.install_proc = None
        if code == 0 and os.path.exists(self.python_path()):
            self._set_install_status()
            self.Log("install finished")
        else:
            self._set_install_status("失敗（logs/install.log）: " + last[:80])
            self.Log("install failed (exit %s): %s" % (code, last))

    def worker_script(self):
        return os.path.join(self.code_dir(), "worker", "mvsync_worker.py")

    # ================================================================== config
    def WriteConfig(self):
        p = self.owner.par
        path = self.config_path()
        cfg = {}
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        cfg.update({
            "library_dir": p.Libraryfolder.eval(),
            "ytdlp": self.ytdlp_path(),
            "node_dir": self.node_dir(),
            "python": self.python_path(),
            "audio_device": p.Audiodevice.eval(),
            "osc_to_td": "127.0.0.1:%d" % int(p.Osctoport.eval()),
            "osc_from_td": int(p.Oscfromport.eval()),
            "recognizer": p.Recognizer.eval(),
            "online_fallback": bool(p.Onlinefallback.eval()),
            "theta_lock": float(p.Thetalock.eval()),
            "theta_hold": float(p.Thetahold.eval()),
            "theta_margin": float(p.Thetamargin.eval()),
            "online_early_s": float(p.Onlineearly.eval()) if hasattr(p, "Onlineearly") else 2.0,
            "stall_reset_s": float(p.Stallreset.eval()) if hasattr(p, "Stallreset") else 5.0,
            "video_quality": int(p.Videoquality.eval()) if hasattr(p, "Videoquality") else 720,
            "official_only": bool(p.Officialonly.eval()) if hasattr(p, "Officialonly") else True,
            "score_log": bool(p.Scorelog.eval()),
        })
        for name, key in (("Prioritywords", "title_priority_words"), ("Ngwords", "title_ng_words")):
            if hasattr(p, name):
                cfg[key] = _words(getattr(p, name).eval())
        cfg.setdefault("ytdlp_args", [])   # the worker adds --js-runtimes (bundled Deno, else node)
        cfg.setdefault("audd_token", "")
        x = float(p.Speedrange.eval()) / 100.0 if hasattr(p, "Speedrange") else 0.16
        cfg["rate_range"] = [round(1 - x, 4), round(1 + x, 4)]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=1, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            self.Log("config write failed: %s" % e)
        return path

    def LoadConfig(self):
        """Pull values from an existing config.json into the Config page."""
        try:
            with open(self.config_path(), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        p = self.owner.par
        # paths (yt-dlp / node / Python) are not pulled back: an empty field keeps auto-detect
        m = {"audio_device": p.Audiodevice, "recognizer": p.Recognizer,
             "theta_lock": p.Thetalock, "theta_hold": p.Thetahold,
             "theta_margin": p.Thetamargin}
        if hasattr(p, "Onlineearly"):
            m["online_early_s"] = p.Onlineearly
        if hasattr(p, "Stallreset"):
            m["stall_reset_s"] = p.Stallreset
        if hasattr(p, "Videoquality"):
            m["video_quality"] = p.Videoquality
        for name, key in (("Prioritywords", "title_priority_words"), ("Ngwords", "title_ng_words")):
            if hasattr(p, name) and isinstance(cfg.get(key), list):
                getattr(p, name).val = ", ".join(cfg[key])
        for k, par in m.items():
            if k in cfg:
                try:
                    par.val = str(cfg[k]) if par.isMenu else cfg[k]
                except Exception:
                    pass

    # ================================================================== worker process
    def _alive(self):
        if self.proc is not None:
            return self.proc.poll() is None
        pid = self.owner.fetch("worker_pid", 0, search=False)
        if not pid:
            return False
        if IS_WIN:
            return time.time() - self.last_hb < 3.0
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def Start(self, auto=False):
        if self._alive():
            self.Log("worker already running")
            self.Send("/mvsync/resend")
            return
        if not auto:
            self.restarts = 0
        self.silent = False           # a new worker only reports silence when it hears it
        cfg = self.WriteConfig()
        py = self.python_path()
        script = self.worker_script()
        if not os.path.exists(py):
            self.Log("Python が見つかりません: %s（Worker ページの Install を押してください）" % py)
            self._set_install_status()
            return
        if not os.path.exists(script):
            self.Log("worker script not found: %s" % script)
            return
        logdir = os.path.join(_x(self.owner.par.Libraryfolder.eval()), "logs")
        os.makedirs(logdir, exist_ok=True)
        out = open(os.path.join(logdir, "worker_stdout.log"), "a", encoding="utf-8")
        kw = {}
        if IS_WIN:
            kw["creationflags"] = 0x00000200 | 0x08000000   # NEW_PROCESS_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("PYTHONHOME", None)    # TD's own Python must not leak into the venv
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen([py, script, "--config", cfg], stdout=out,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     cwd=os.path.dirname(script), env=env, **kw)
        self.owner.store("worker_pid", self.proc.pid)
        self.last_hb = time.time()     # grace period
        self.owner.store("last_hb", self.last_hb)
        self.Log("worker started (pid %d)" % self.proc.pid)

    def Stop(self, kill_after=1.5):
        self.Send("/mvsync/quit")
        pid = self.proc.pid if self.proc is not None else self.owner.fetch("worker_pid", 0, search=False)
        if pid:
            run("args[0]._kill(args[1])", self, int(pid), delayMilliSeconds=int(kill_after * 1000))
        self.Log("worker stop requested")

    def _kill_tree_win(self, pid):
        """Windows: end the worker and its yt-dlp / ffmpeg children (no process groups there)."""
        try:
            subprocess.Popen(["taskkill", "/PID", str(int(pid)), "/T", "/F"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=0x08000000)
        except Exception:
            pass

    def _kill(self, pid):
        try:
            if self.proc is not None and self.proc.pid == pid:
                if self.proc.poll() is None:
                    if IS_WIN:
                        self._kill_tree_win(pid)
                    else:
                        self.proc.terminate()
                self.proc = None
            elif IS_WIN:
                self._kill_tree_win(pid)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
        if self.owner.fetch("worker_pid", 0, search=False) == pid:
            self.owner.unstore("worker_pid")

    def KillNow(self):
        """Called on tox destroy / TD exit."""
        self.Send("/mvsync/quit")
        pid = self.proc.pid if self.proc is not None else self.owner.fetch("worker_pid", 0, search=False)
        if pid:
            self._kill(int(pid))

    def OnExit(self):
        """TD is quitting: stop the worker (and its yt-dlp / ffmpeg children), then delete the
        cache = every downloaded MV that is not in a playlist (online finds, single urls.txt lines)."""
        pid = self.proc.pid if self.proc is not None else self.owner.fetch("worker_pid", 0, search=False)
        self.KillNow()
        if pid and not IS_WIN:
            try:
                os.killpg(int(pid), 15)       # worker runs in its own session/process group
            except Exception:
                pass
        if not getattr(self.owner.par, "Clearcache", None) or not self.owner.par.Clearcache.eval():
            return
        if self.proc is not None:
            try:
                self.proc.wait(timeout=2.0)
            except Exception:
                pass
        else:
            time.sleep(0.5)
        self.PurgeCache()

    def PurgeCache(self):
        import shutil
        root = os.path.join(_x(self.owner.par.Libraryfolder.eval()), "library")
        n, size = 0, 0
        for vid in (os.listdir(root) if os.path.isdir(root) else []):
            d = os.path.join(root, vid)
            if not os.path.isdir(d):
                continue
            try:
                with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                    m = json.load(f)
            except Exception:
                m = {}          # no meta.json: leftover folder, not a library entry
            # keep playlist videos, and "removed" markers (they stop a re-download)
            if m.get("playlists") or m.get("status") == "removed":
                continue
            for fn in os.listdir(d):
                try:
                    size += os.path.getsize(os.path.join(d, fn))
                except Exception:
                    pass
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        self.Log("cache cleared: %d MV(s), %.0f MB" % (n, size / 1e6))
        return n

    def onDestroyTD(self):
        """Called on COMP deletion *and* on extension re-init (code reload). Only stop the
        worker when the COMP itself is gone: check one frame later from the root."""
        pid = self.proc.pid if self.proc is not None else self.owner.fetch("worker_pid", 0, search=False)
        if not pid:
            return
        port = int(self.owner.par.Oscfromport.eval())
        script = (
            "import os, signal, socket, struct\n"
            "if op(args[0]) is None:\n"
            "    try:\n"
            "        m = b'/mvsync/quit' + b'\\x00' * (4 - len('/mvsync/quit') % 4) + b',\\x00\\x00\\x00'\n"
            "        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(m, ('127.0.0.1', args[2]))\n"
            "    except Exception:\n"
            "        pass\n"
            "    try:\n"
            "        os.kill(args[1], signal.SIGTERM)\n"
            "    except Exception:\n"
            "        pass\n")
        run(script, self.owner.path, int(pid), port, fromOP=op("/"), delayFrames=2)

    def UpdateTools(self):
        if self.update_proc is not None and self.update_proc.poll() is None:
            self.Log("update already running")
            return
        self.Stop(kill_after=0.5)
        cfg = self.WriteConfig()
        py = self.python_path()
        kw = {"start_new_session": True} if not IS_WIN else {"creationflags": 0x08000000}
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        logdir = os.path.join(_x(self.owner.par.Libraryfolder.eval()), "logs")
        os.makedirs(logdir, exist_ok=True)
        out = open(os.path.join(logdir, "update_tools.log"), "a", encoding="utf-8")
        self.update_proc = subprocess.Popen([py, self.worker_script(), "--config", cfg, "--update-tools"],
                                            stdout=out, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, env=env, **kw)
        self.Log("Update tools: running pip -U shazamio / yt-dlp ...")

    # ================================================================== OSC in
    def OnOSC(self, address, args):
        a = args
        if address == "/mvsync/est" and len(a) >= 5:
            self.est = {"id": str(a[0]), "pos": float(a[1]), "rate": float(a[2]),
                        "conf": float(a[3]), "t_ref": float(a[4])}
        elif address == "/mvsync/heartbeat":
            now = time.time()
            self.last_hb = now
            self.owner.store("last_hb", now)
            if a:
                off = float(a[0]) - now
                self.clk_off = off if abs(off - self.clk_off) > 1.0 else 0.9 * self.clk_off + 0.1 * off
        elif address == "/mvsync/state" and a:
            self.state = str(a[0])
            self.owner.par.State = self.state
        elif address == "/mvsync/play" and len(a) >= 2:
            self.Play(str(a[0]), str(a[1]))
        elif address == "/mvsync/track_changed":
            self.track_changed_frame = True
            self.nudge_ms = 0.0
            self.owner.par.Nudge = 0.0
        elif address == "/mvsync/unload" and a:
            self.Unload(str(a[0]))
        elif address == "/mvsync/library" and a:
            self.LoadLibrary(str(a[0]))
        elif address == "/mvsync/scores" and len(a) >= 5:
            p = self.owner.par
            p.Score1, p.Score2, p.Ratio, p.Trackscore = float(a[1]), float(a[2]), float(a[3]), float(a[4])
        elif address == "/mvsync/online" and len(a) >= 4:
            txt = "%s - %s" % (a[0], a[1]) if a[0] or a[1] else ""
            if a[2]:
                txt += "  ->  %s" % a[2]
            self.owner.par.Online = "[%s] %s" % (ONLINE_LABELS.get(str(a[3]), a[3]), txt)
            self.Log("online %s: %s" % (a[3], txt))
        elif address == "/mvsync/versions" and len(a) >= 2:
            self.owner.par.Ytdlpversion = str(a[0])
            self.owner.par.Shazamversion = str(a[1])
        elif address == "/mvsync/confirm" and len(a) >= 3:
            self.owner.par.Confirmplaylist = "%s|%s (%d videos)" % (a[0], a[1], int(a[2]))
            self.Log("プレイリスト '%s' は %d 本あります。Library ページで取り込みを確認してください" % (a[1], int(a[2])))
        elif address == "/mvsync/silence" and a:
            self.silent = bool(int(a[0]))
        elif address == "/mvsync/bpm" and len(a) >= 2:
            self.bpm_base[str(a[0])] = float(a[1])
        elif address == "/mvsync/log" and a:
            self.Log(str(a[0]))

    # ================================================================== library
    def LoadLibrary(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                lib = json.load(f)
        except Exception as e:
            self.Log("library read failed: %s" % e)
            return
        self.library = lib
        t = self._op("library")
        t.clear()
        t.appendRow(["id", "title", "status", "source", "duration", "playlists", "reason"])
        names, labels = ["auto"], ["(auto)"]
        for e in lib.get("entries", []):
            pls = ", ".join("%s#%s" % (p.get("title") or p.get("id"), p.get("index"))
                            for p in (e.get("playlists") or []))
            t.appendRow([e.get("id"), e.get("title") or "", e.get("status"), e.get("source"),
                         e.get("duration") or "", pls, (e.get("reason") or "")[:200]])
            self.titles[e.get("id")] = e.get("title") or e.get("id")
            if e.get("status") == "ready":
                names.append(e["id"])
                labels.append((e.get("title") or e["id"])[:80])
        p = self.owner.par
        # Menus come from table DATs (menuSource). Writing menuNames/menuLabels directly is a
        # parameter-definition change, which makes TD re-create this extension on every
        # library update (lost heartbeat -> "worker stopped", lost playback state).
        self._fill_menu("menu_tracks", names, labels)
        pn, pl = ["All"], ["All"]
        for pli in lib.get("playlists", []):
            pn.append(pli["id"])
            pl.append((pli.get("title") or pli["id"])[:80])
        self._fill_menu("menu_playlists", pn, pl)
        n_ready = sum(1 for e in lib.get("entries", []) if e.get("status") == "ready")
        p.Libraryinfo = "%d ready / %d total, %d playlists" % (n_ready, len(lib.get("entries", [])),
                                                               len(lib.get("playlists", [])))

    def _fill_menu(self, name, names, labels):
        t = self._op(name)
        if t is None:
            return
        rows = [[n, l] for n, l in zip(names, labels)]
        if [[c.val for c in r] for r in t.rows()] == rows:
            return
        t.clear()
        for r in rows:
            t.appendRow(r)

    # ================================================================== playback
    def Play(self, vid, src):
        """Worker says: show this MV (local file or stream URL)."""
        if not src:
            return
        if src.startswith("http") and "tdext=" not in src:
            src += "&tdext=.mp4"
        cur = self.slots[self.active]
        if cur["id"] == vid and cur["src"] == src:
            return
        slot = "B" if self.active == "A" else "A"
        s = self.slots[slot]
        s.update(id=vid, src=src, opened=time.time(), ph=cur["ph"] if cur["id"] == vid else 0.0,
                 rate=cur["rate"] if cur["id"] == vid else 1.0)
        mov = self._op("mov" + slot)
        mov.par.file = src
        self.pending = slot
        self.owner.par.Trackid = vid
        self.owner.par.Track = self.titles.get(vid, vid)

    def Unload(self, vid):
        """The MV was deleted: take it off screen (black until the next MV)."""
        for name, s in self.slots.items():
            if s["id"] == vid:
                self.slots[name] = self._empty_slot()
                self._op("mov" + name).par.file = ""
        if self.pending and not self.slots[self.pending]["id"]:
            self.pending = None
        self.fade = None
        if self.owner.par.Trackid.eval() == vid:
            self.owner.par.Trackid = ""
            self.owner.par.Track = ""
        if self.est and self.est.get("id") == vid:
            self.est = None

    def _slot_ready(self, slot):
        info = self._op("info" + slot)
        try:
            return info["open"].eval() >= 1 and info["opening"].eval() < 1
        except Exception:
            return False

    def _target(self, slot, now, dt):
        """Desired MV position for a slot this frame (spec §6.3 play clock)."""
        s = self.slots[slot]
        p = self.owner.par
        e = self.est
        if p.Sync.eval() and e and e["id"] == s["id"]:
            t_w = now + self.clk_off
            tgt = e["pos"] + e["rate"] * (t_w - e["t_ref"])
            tgt += (p.Offset.eval() + self.nudge_ms) / 1000.0
            return tgt, e["rate"]
        return s["ph"] + s["rate"] * dt, s["rate"]

    def _advance(self, slot, now, dt):
        s = self.slots[slot]
        if not s["id"]:
            return
        if (self.state == "IDLE" or self.silent) and self.owner.par.Idleoutput.eval() == "last":
            return       # freeze on the last frame while nothing is playing
        if not self.owner.par.Sync.eval():
            s["rate"] = 1.0
            s["ph"] += dt
            return
        tgt, rate = self._target(slot, now, dt)
        err = tgt - s["ph"]
        s["rate"] = rate
        if abs(err) < 0.020:
            s["ph"] += rate * dt
        elif abs(err) < 0.250:
            corr = max(-0.03, min(0.03, err * 0.5)) * rate
            s["ph"] += (rate + corr) * dt
        else:
            s["ph"] = tgt
        dur = s["dur"]
        if dur > 0:
            s["ph"] = max(0.0, min(s["ph"], dur - 0.05))
        else:
            s["ph"] = max(0.0, s["ph"])

    def Tick(self):
        now = time.time()
        dt = max(0.0, min(0.25, now - self.last_tick))
        self.last_tick = now
        p = self.owner.par
        # pending track change: wait until the other Movie File In has opened, then fade
        if self.pending:
            slot = self.pending
            if self._slot_ready(slot) or now - self.slots[slot]["opened"] > 4.0:
                info = self._op("info" + slot)
                try:
                    self.slots[slot]["dur"] = info["true_length"].eval() / max(1.0, info["sample_rate"].eval())
                except Exception:
                    self.slots[slot]["dur"] = 0.0
                # take over position if same MV (stream -> local file)
                self.fade = (self.active, slot, now)
                self.active = slot
                self.pending = None
        for slot in ("A", "B"):
            self._advance(slot, now, dt)
            self._op("mov" + slot).par.index = self.slots[slot]["ph"]
        # crossfade 0.5 s
        x = self._op("cross")
        target = 0.0 if self.active == "A" else 1.0
        if self.fade:
            k = min(1.0, (now - self.fade[2]) / 0.5)
            x.par.cross = (1 - k) * (1 - target) + k * target
            if k >= 1.0:
                old = self.fade[0]
                self.fade = None
                if old != self.active and self.slots[old]["id"] != self.slots[self.active]["id"]:
                    pass
        else:
            x.par.cross = target
        # output: 0 = video, 1 = black
        has = bool(self.slots[self.active]["id"])
        black = (not has) or ((self.state == "IDLE" or self.silent) and p.Idleoutput.eval() == "black")
        idx = 1 if black else 0
        self._op("outsw").par.index = idx
        # displayed position check (Info CHOP last_index_uploaded / sample_rate)
        try:
            info = self._op("info" + self.active)
            shown = info["last_index_uploaded"].eval() / max(1.0, info["sample_rate"].eval())
            p.Lagms = (self.slots[self.active]["ph"] - shown) * 1000.0
        except Exception:
            pass
        # heartbeat watchdog + auto restart once
        alive_hb = now - self.last_hb < 3.0
        p.Running = alive_hb
        if not alive_hb and self.owner.fetch("worker_pid", 0, search=False):
            if p.Workerstatus.eval() != "停止":
                p.Workerstatus = "停止"
                self.Log("ワーカー停止（heartbeat が 3 秒来ていません）")
                if p.Autorestart.eval() and self.restarts < 1 and not self._alive():
                    self.restarts += 1
                    self.Log("ワーカーを自動再起動します（1 回まで）")
                    self.owner.unstore("worker_pid")
                    self.proc = None
                    self.Start(auto=True)
        elif alive_hb:
            p.Workerstatus = "動作中"
        self._poll_install(now)
        if self.update_proc is not None and self.update_proc.poll() is not None:
            self.Log("Update tools finished (exit %s)" % self.update_proc.returncode)
            self.update_proc = None
        # out_ctrl
        e = self.est or {}
        p.Conf = e.get("conf", 0.0)
        p.Rate = e.get("rate", 1.0)
        c = self._op("ctrl")
        st = STATES.index(self.state) if self.state in STATES else -1
        bpm = self.bpm_base.get(e.get("id", ""), 0.0) * e.get("rate", 1.0)
        c.par.const0value = st
        c.par.const1value = e.get("conf", 0.0)
        c.par.const2value = e.get("rate", 1.0)
        c.par.const3value = bpm
        c.par.const4value = 1 if self.state == "LOCKED" else 0
        c.par.const5value = 1 if self.track_changed_frame else 0
        self.track_changed_frame = False

    # ================================================================== parameters
    def OnPulse(self, par):
        n = par.name
        if n == "Install":
            self.Install()
        elif n == "Start":
            self.Start()
        elif n == "Stop":
            self.Stop()
        elif n == "Relock":
            self.Send("/mvsync/relock")
        elif n == "Reject":
            self.Send("/mvsync/reject")
        elif n == "Nudgeminus":
            self._nudge(-10)
        elif n == "Nudgeplus":
            self._nudge(10)
        elif n == "Markok":
            self.Send("/mvsync/mark", "ok")
            self.Log("Mark OK")
        elif n == "Markng":
            self.Send("/mvsync/mark", "ng")
            self.Log("Mark NG")
        elif n == "Refreshplaylists":
            self.Send("/mvsync/refresh_playlists")
        elif n == "Retryfailed":
            for e in self.library.get("entries", []):
                if e.get("status") == "failed":
                    self.Send("/mvsync/retry", e["id"])
        elif n == "Upgradevideos":
            self.Send("/mvsync/upgrade_videos")
            self.Log("%sp 未満の MV を取り直します（裏で 1 本ずつ）" % self.owner.par.Videoquality.eval())
        elif n == "Removeentry":
            p = self.owner.par
            vid = (p.Removeselect.eval() if hasattr(p, "Removeselect") else p.Removeid.eval()).strip()
            if not vid or vid == "auto":
                self.Log("削除する MV を Library ページの「MV to remove」で選んでください")
                return
            self.Send("/mvsync/remove", vid)
            self.Log("remove %s %s" % (vid, self.titles.get(vid, "")))
            if hasattr(p, "Removeselect"):
                p.Removeselect = "auto"
        elif n in ("Confirmyes", "Confirmno"):
            pid = self.owner.par.Confirmplaylist.eval().split("|")[0]
            if pid:
                self.Send("/mvsync/confirm_reply", pid, 1 if n == "Confirmyes" else 0)
                self.owner.par.Confirmplaylist = ""
        elif n == "Updatetools":
            self.UpdateTools()
        elif n == "Writeconfig":
            self.Log("config written: %s" % self.WriteConfig())
        elif n == "Openfolder":
            folder = _x(self.owner.par.Libraryfolder.eval())
            try:
                if IS_WIN:
                    os.startfile(folder)
                else:
                    subprocess.Popen(["open", folder])
            except Exception as e:
                self.Log(str(e))

    def _nudge(self, ms):
        self.nudge_ms += ms
        self.owner.par.Nudge = self.nudge_ms

    def OnValueChange(self, par):
        n = par.name
        if n == "Sync":
            self.Send("/mvsync/sync", 1 if par.eval() else 0)
        elif n == "Manualselect":
            v = par.eval()
            self.Send("/mvsync/select", "" if v == "auto" else v)
            if v != "auto" and not self.owner.par.Sync.eval():
                f = os.path.join(_x(self.owner.par.Libraryfolder.eval()), "library", v, "video.mp4")
                self.Play(v, f)
        elif n == "Activeplaylist":
            self.Send("/mvsync/active_playlist", par.eval())
        elif n in ("Thetalock", "Thetahold", "Thetamargin", "Onlineearly", "Stallreset"):
            key = {"Thetalock": "theta_lock", "Thetahold": "theta_hold",
                   "Thetamargin": "theta_margin", "Onlineearly": "online_early_s",
                   "Stallreset": "stall_reset_s"}[n]
            self.Send("/mvsync/param", key, float(par.eval()))
            self.WriteConfig()
        elif n == "Speedrange":
            self.Send("/mvsync/param", "rate_range_pct", float(par.eval()))
            self.WriteConfig()
        elif n == "Videoquality":
            self.Send("/mvsync/param", "video_quality", float(par.eval()))
            self.WriteConfig()
        elif n in ("Prioritywords", "Ngwords"):
            key = {"Prioritywords": "title_priority_words", "Ngwords": "title_ng_words"}[n]
            self.Send("/mvsync/param", key, ", ".join(_words(par.eval())))
            self.WriteConfig()
        elif n in ("Onlinefallback", "Scorelog", "Officialonly"):
            key = {"Onlinefallback": "online_fallback", "Scorelog": "score_log",
                   "Officialonly": "official_only"}[n]
            self.Send("/mvsync/param", key, 1.0 if par.eval() else 0.0)
            self.WriteConfig()
        elif par.page.name == "Config":
            self.WriteConfig()
