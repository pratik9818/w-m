"""Reading back the record the bot already keeps.

Everything in this package is offline. Nothing here runs while an owner is waiting, and
nothing here makes a model call, so the loop costs nothing per message -- which is the
only reason it can be afforded at this traffic.
"""
