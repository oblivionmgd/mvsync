# Parameter Execute DAT on the mvsync COMP (custom pars only)

def onValueChange(par, prev):
	par.owner.ext.MVSync.OnValueChange(par)
	return

def onPulse(par):
	par.owner.ext.MVSync.OnPulse(par)
	return
