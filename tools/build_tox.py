"""Builds the release mvsync.tox inside a running TouchDesigner.

Run from the Textport (or any exec API) with the repository root as ROOT:

    ROOT = '/path/to/mvsync'; exec(open(ROOT + '/tools/build_tox.py', encoding='utf-8').read())

It builds the COMP in a temporary base COMP, saves <ROOT>/dist/mvsync.tox and destroys the
temporary COMP in the same script run, so nothing in the open project cooks or changes
(the OSC In DAT never binds its port). Scripts are embedded in the tox (RELEASE=True).
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
    _c.par.externaltox = ""
    _c.par.enableexternaltox = False
    for _k in list(_c.storage.keys()):
        _c.unstore(_k)
    os.makedirs(os.path.dirname(_out), exist_ok=True)
    _c.save(_out)
    result = _out
finally:
    _tmp.destroy()
