"""Builds the release mvsync.tox inside a running TouchDesigner.

Run from the Textport (or any exec API) with the repository root as ROOT:

    ROOT = '/path/to/mvsync'; VERSION = '0.1.0'; exec(open(ROOT + '/tools/build_tox.py', encoding='utf-8').read())

It builds the COMP in a temporary base COMP, saves <ROOT>/dist/mvsync.tox and destroys the
temporary COMP in the same script run, so nothing in the open project cooks or changes
(the OSC In DAT never binds its port). Scripts are embedded in the tox (RELEASE=True), and the
worker / setup files go into mvsync/payload for the Install button.
"""
import os

_root = os.path.abspath(os.path.expanduser(globals().get("ROOT") or os.getcwd()))
_out = globals().get("OUT") or os.path.join(_root, "dist", "mvsync.tox")
_tmp = op("/").create(baseCOMP, "_mvsync_release_build")
try:
    _g = dict(globals())
    _g.update({"CODEDIR": _root, "PARENT": _tmp.path, "RELEASE": True, "LIBRARY": "~/MVSync"})
    exec(open(os.path.join(_root, "td", "build_mvsync.py"), encoding="utf-8").read(), _g)
    _c = _tmp.op("mvsync")
    # embedded worker + setup files: the Install button writes them to <Library folder>/app
    _ver = str(globals().get("VERSION") or "dev")
    _pl = _c.create(baseCOMP, "payload")
    _pl.nodeX, _pl.nodeY = -600, -450
    _idx = _pl.create(tableDAT, "files")
    _idx.clear()
    _idx.appendRow(["path", "dat", "version=" + _ver])
    _rels = sorted("worker/" + f for f in os.listdir(os.path.join(_root, "worker"))
                   if f.endswith(".py") or f == "requirements.txt") + ["setup.sh", "setup.bat"]
    for _i, _rel in enumerate(_rels):
        _d = _pl.create(textDAT, "f%02d" % _i)
        with open(os.path.join(_root, *_rel.split("/")), encoding="utf-8", newline="") as _f:
            _d.text = _f.read().replace("\r\n", "\n")
        _d.nodeX, _d.nodeY = 0, -120 * _i
        _idx.appendRow([_rel, _d.name])
    _c.par.externaltox = ""
    _c.par.enableexternaltox = False
    for _k in list(_c.storage.keys()):
        _c.unstore(_k)
    os.makedirs(os.path.dirname(_out), exist_ok=True)
    _c.save(_out)
    result = _out
finally:
    _tmp.destroy()
