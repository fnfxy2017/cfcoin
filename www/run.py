#!flask/bin/python
import random
import sys
sys.path.append('.')
sys.path.append('..')
from socketInfo.CoinSocket import ReivSocket, SendSocket
from www.app import app






SendSocket.init()
app.run(debug = True)
