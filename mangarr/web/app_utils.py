import time


def fmt_ts(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "jamais"
