# OSC In DAT callbacks -> MVSync extension (worker -> TD, spec §8)

def onReceiveOSC(dat, rowIndex, message, byteData, timeStamp, address, args, peer):
	try:
		dat.parent().ext.MVSync.OnOSC(address, args)
	except Exception as e:
		debug('mvsync OSC error', address, e)
	return
