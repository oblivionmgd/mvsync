"""Builds /project1/mvsync inside a running TouchDesigner (run through the MCP exec API or the
Textport: exec(open(path).read())). Re-running rebuilds the COMP from scratch.

CODEDIR must point at the repository root (the folder holding td/ and worker/).
Optional globals: PARENT, LIBRARY, RELEASE (see below).
"""
import os

CODEDIR = globals().get("CODEDIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = globals().get("PARENT", "/project1")
LIBRARY = globals().get("LIBRARY", "~/MVSync")
# RELEASE=True: scripts are embedded in the COMP (no file sync) so the saved tox works anywhere
RELEASE = bool(globals().get("RELEASE", False))

parent_op = op(PARENT)
old = parent_op.op("mvsync")
if old is not None:
    try:
        old.ext.MVSync.KillNow()
    except Exception:
        pass
    old.destroy()

c = parent_op.create(containerCOMP, "mvsync")
c.nodeX, c.nodeY = 0, -200
c.color = (0.25, 0.4, 0.6)
c.par.w, c.par.h = 640, 360

# ---------------------------------------------------------------- custom parameters
def page(name):
    return c.appendCustomPage(name)

pg = page("Control")
pg.appendToggle("Sync", label="Sync")[0].default = True
p = pg.appendFloat("Offset", label="Offset (ms)")[0]
p.normMin, p.normMax, p.default = -500, 500, 0
pg.appendPulse("Nudgeminus", label="Nudge -10ms")
pg.appendPulse("Nudgeplus", label="Nudge +10ms")
p = pg.appendFloat("Nudge", label="Nudge (ms)")[0]
p.readOnly = True
pg.appendPulse("Relock", label="Relock")
pg.appendPulse("Reject", label="Reject current MV")
p = pg.appendMenu("Manualselect", label="Manual select")[0]
p.menuSource = "tdu.TableMenu(op('./menu_tracks'), labelCol=1, includeFirstRow=True)"   # filled by LoadLibrary
p.default = "auto"
p = pg.appendMenu("Idleoutput", label="When silent")[0]
p.menuNames, p.menuLabels, p.default = ["black", "last"], ["Black", "Last frame"], "black"
p = pg.appendInt("Outputres", label="Output Resolution", size=2)
p[0].default, p[1].default = 1280, 720
pg.appendPulse("Markok", label="Mark OK (correct now)")
pg.appendPulse("Markng", label="Mark NG (wrong now)")

pg = page("Status")
for name, label in [("State", "State"), ("Track", "Track"), ("Trackid", "Track id"),
                    ("Online", "Online"), ("Lastlog", "Last log"), ("Workerstatus", "Worker")]:
    p = pg.appendStr(name, label=label)[0]
    p.readOnly = True
for name, label in [("Conf", "Confidence"), ("Rate", "Rate"), ("Score1", "1st score"),
                    ("Score2", "2nd score"), ("Ratio", "1st / 2nd"), ("Trackscore", "Tracking score"),
                    ("Lagms", "Shown vs clock (ms)")]:
    p = pg.appendFloat(name, label=label)[0]
    p.readOnly = True

pg = page("Library")
p = pg.appendMenu("Activeplaylist", label="Active playlist")[0]
p.menuSource = "tdu.TableMenu(op('./menu_playlists'), labelCol=1, includeFirstRow=True)"
p.default = "All"
pg.appendPulse("Refreshplaylists", label="Refresh playlists")
pg.appendPulse("Retryfailed", label="Retry failed")
p = pg.appendStr("Libraryinfo", label="Library")[0]
p.readOnly = True
p = pg.appendStr("Confirmplaylist", label="Confirm playlist")[0]
p.readOnly = True
pg.appendPulse("Confirmyes", label="Import big playlist")
pg.appendPulse("Confirmno", label="Skip big playlist")
p = pg.appendMenu("Removeselect", label="MV to remove")[0]
p.menuSource = "tdu.TableMenu(op('./menu_tracks'), labelCol=1, includeFirstRow=True)"   # (auto) = none
p.default = "auto"
pg.appendPulse("Removeentry", label="Remove selected MV")
pg.appendPulse("Openfolder", label="Open library folder")
pg.appendToggle("Clearcache", label="Clear cache on exit")[0].default = True   # non-playlist MVs

pg = page("Worker")
pg.appendPulse("Install", label="Install / Repair")      # runs setup.sh / setup.bat: Python, yt-dlp, ffmpeg, Deno
p = pg.appendStr("Installstatus", label="Install status")[0]
p.readOnly = True
pg.appendPulse("Start", label="Start")
pg.appendPulse("Stop", label="Stop")
p = pg.appendToggle("Running", label="Running")[0]
p.readOnly = True
pg.appendToggle("Autorestart", label="Auto restart once")[0].default = True
pg.appendPulse("Updatetools", label="Update tools (yt-dlp / ShazamIO)")
for name, label in [("Ytdlpversion", "yt-dlp version"), ("Shazamversion", "ShazamIO version")]:
    p = pg.appendStr(name, label=label)[0]
    p.readOnly = True

pg = page("Config")
pg.appendFolder("Libraryfolder", label="Library folder")[0].default = LIBRARY
pg.appendFile("Ytdlppath", label="yt-dlp path (empty = auto)")[0].default = ""
pg.appendFolder("Nodedir", label="node folder (empty = auto)")[0].default = ""
pg.appendFile("Pythonpath", label="Worker Python (empty = auto)")[0].default = ""
pg.appendStr("Audiodevice", label="Audio device (empty = OS default)")[0].default = ""
pg.appendInt("Osctoport", label="OSC port worker->TD")[0].default = 9100
pg.appendInt("Oscfromport", label="OSC port TD->worker")[0].default = 9101
p = pg.appendMenu("Recognizer", label="Recognizer")[0]
p.menuNames, p.menuLabels, p.default = ["shazamio", "audd"], ["ShazamIO", "AudD"], "shazamio"
pg.appendToggle("Onlinefallback", label="Online fallback")[0].default = True
pg.appendToggle("Officialonly", label="Official MVs only")[0].default = True   # online: no fan uploads
for name, label, d in [("Thetalock", "theta_lock", 0.6), ("Thetahold", "theta_hold", 0.45),
                       ("Thetamargin", "theta_margin", 0.03)]:
    p = pg.appendFloat(name, label=label)[0]
    p.default, p.normMin, p.normMax = d, 0.0, 1.0
p = pg.appendFloat("Speedrange", label="Speed range ±%")[0]
p.default, p.normMin, p.normMax = 16.0, 4.0, 25.0
p = pg.appendFloat("Onlineearly", label="Online lookup after (s)")[0]
p.default, p.normMin, p.normMax = 2.0, 0.0, 30.0
p = pg.appendFloat("Stallreset", label="Stall reset (s)")[0]
p.default, p.normMin, p.normMax = 5.0, 0.0, 20.0
p = pg.appendMenu("Videoquality", label="Video quality")[0]
p.menuNames, p.menuLabels, p.default = ["360", "720", "1080"], ["360p", "720p", "1080p"], "720"
pg.appendPulse("Upgradevideos", label="Re-download below this quality")
pg.appendToggle("Scorelog", label="Write scores CSV")[0].default = True
# online search title words (comma-separated; defaults = worker/title_words.py)
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("title_words", os.path.join(CODEDIR, "worker", "title_words.py"))
_tw = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_tw)
pg.appendStr("Prioritywords", label="Priority words")[0].default = _tw.to_text(_tw.PRIORITY_WORDS)
pg.appendStr("Ngwords", label="NG words")[0].default = _tw.to_text(_tw.NG_WORDS)
pg.appendFolder("Codedir", label="Code folder (empty = auto)")[0].default = "" if RELEASE else CODEDIR
pg.appendPulse("Writeconfig", label="Write config.json")

for p in c.customPars:
    if not p.readOnly:
        try:
            p.val = p.default
        except Exception:
            pass

# ---------------------------------------------------------------- scripts (synced to files)
def link(d, rel):
    if RELEASE:
        d.text = open(os.path.join(CODEDIR, "td", rel), encoding="utf-8").read()
    else:
        d.par.file.expr = "parent().par.Codedir + '/td/%s'" % rel
        d.par.syncfile = True


def text(name, rel, x, y):
    d = c.create(textDAT, name)
    link(d, rel)
    d.nodeX, d.nodeY = x, y
    return d

ext = text("mvsync_ext", "mvsync_ext.py", -600, 300)
oscb = text("osc_callbacks", "osc_callbacks.py", -600, 150)
fexe = c.create(executeDAT, "frame_exec")
link(fexe, "frame_exec.py")
fexe.par.framestart = True
fexe.par.exit = True
fexe.nodeX, fexe.nodeY = -600, 0
pexe = c.create(parameterexecuteDAT, "par_exec")
link(pexe, "par_exec.py")
pexe.par.op = ".."
pexe.par.pars = "*"
pexe.par.builtin = False
pexe.par.custom = True
pexe.par.valuechange = True
pexe.par.onpulse = True
pexe.nodeX, pexe.nodeY = -600, -150

# ---------------------------------------------------------------- OSC
oin = c.create(oscinDAT, "oscin")
oin.par.port.expr = "parent().par.Osctoport"
oin.par.callbacks = oscb.name
for d in c.findChildren(name="oscin_callbacks*"):
    d.destroy()
oin.nodeX, oin.nodeY = -400, 150
oout = c.create(oscoutDAT, "oscout")
oout.par.address = "127.0.0.1"
oout.par.port.expr = "parent().par.Oscfromport"
oout.nodeX, oout.nodeY = -400, 0

log = c.create(tableDAT, "log")
log.clear()
log.nodeX, log.nodeY = -400, -150
lib = c.create(tableDAT, "library")
lib.clear()
lib.nodeX, lib.nodeY = -400, -300
for i, (name, row) in enumerate([("menu_tracks", ["auto", "(auto)"]), ("menu_playlists", ["All", "All"])]):
    m = c.create(tableDAT, name)
    m.clear()
    m.appendRow(row)
    m.nodeX, m.nodeY = -200, -300 - i * 150

# ---------------------------------------------------------------- video chain
x0 = 0
fits = []
for i, s in enumerate("AB"):
    m = c.create(moviefileinTOP, "mov" + s)
    m.par.file = ""
    m.par.playmode = "specify"
    m.par.indexunit = "seconds"
    m.par.index = 0
    m.par.predownloadhttp = False
    m.nodeX, m.nodeY = x0, 200 - i * 250
    inf = c.create(infoCHOP, "info" + s)
    inf.par.op = m.name
    inf.nodeX, inf.nodeY = x0, 100 - i * 250
    f = c.create(fitTOP, "fit" + s)
    f.inputConnectors[0].connect(m)
    f.par.outputresolution = "custom"
    f.par.resolutionw.expr = "parent().par.Outputres1"
    f.par.resolutionh.expr = "parent().par.Outputres2"
    f.par.fit = "fitbest"
    f.nodeX, f.nodeY = x0 + 200, 200 - i * 250
    fits.append(f)

cross = c.create(crossTOP, "cross")
cross.inputConnectors[0].connect(fits[0])
cross.inputConnectors[1].connect(fits[1])
cross.nodeX, cross.nodeY = 450, 75
black = c.create(constantTOP, "black")
black.par.outputresolution = "custom"
black.par.resolutionw.expr = "parent().par.Outputres1"
black.par.resolutionh.expr = "parent().par.Outputres2"
black.par.colorr = black.par.colorg = black.par.colorb = 0
black.par.alpha = 1
black.nodeX, black.nodeY = 450, -100
sw = c.create(switchTOP, "outsw")
sw.inputConnectors[0].connect(cross)
sw.inputConnectors[1].connect(black)
sw.nodeX, sw.nodeY = 650, 0
out = c.create(outTOP, "out1")
out.inputConnectors[0].connect(sw)
out.nodeX, out.nodeY = 850, 0

# ---------------------------------------------------------------- control CHOP
ctrl = c.create(constantCHOP, "ctrl")
for i, n in enumerate(["state", "conf", "rate", "bpm_est", "locked", "track_changed"]):
    getattr(ctrl.par, "const%dname" % i).val = n
ctrl.nodeX, ctrl.nodeY = 450, -300
oc = c.create(outCHOP, "out_ctrl")
oc.inputConnectors[0].connect(ctrl)
oc.nodeX, oc.nodeY = 650, -300

# ---------------------------------------------------------------- panel + extension
c.par.top = "./out1"
c.par.extension1 = "op('./mvsync_ext').module.MVSyncExt(me)"
c.par.extname1 = "MVSync"
c.par.promoteextension1 = False
c.initializeExtensions()
c.ext.MVSync.LoadConfig()
result = c.path
