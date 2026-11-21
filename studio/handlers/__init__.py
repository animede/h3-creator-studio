"""ジョブの種類ごとのハンドラ。kind(例: h3image.t2i)から Handler を引く。"""
from . import ace, h3, h3image

HANDLERS = {**h3image.HANDLERS, **h3.HANDLERS, **ace.HANDLERS}
