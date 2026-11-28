"""ジョブの種類ごとのハンドラ。kind(例: h3image.t2i)から Handler を引く。"""
from . import ace, edit, h3, h3image, mv, short, upscale

HANDLERS = {**h3image.HANDLERS, **h3.HANDLERS, **ace.HANDLERS, **mv.HANDLERS, **short.HANDLERS, **edit.HANDLERS, **upscale.HANDLERS}
