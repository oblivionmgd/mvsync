# Execute DAT: per-frame play clock + clean shutdown of the worker

def onFrameStart(frame):
	me.parent().ext.MVSync.Tick()
	return

def onExit():
	me.parent().ext.MVSync.OnExit()
	return
