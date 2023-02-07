from binance.client import Client 
import key


# API key/secret are required for user data endpoints
client = Client(key.api_key,key.api_secret)

#Symbols
BUSDUAH = 'BUSDUAH'
USDTUAH = 'USDTUAH'
BUSDUSDT = 'BUSDUSDT'

#var
delta = 0

#functions
def BUSDUAH_Bid():
    Bid = client.get_order_book(symbol = BUSDUAH)
    #print(str(Bid['bids'][0][0]))
    return Bid['bids'][0][0]

def BUSDUAH_Ask():
    Ask = client.get_order_book(symbol = BUSDUAH)
    #print(str(Ask['asks'][0][0]))
    return Ask['asks'][0][0]

def USDTUAH_Bid():
    Bid = client.get_order_book(symbol = USDTUAH)
    #print(str(Bid['bids'][0][0]))
    return Bid['bids'][0][0]

def USDTUAH_Ask():
    Ask = client.get_order_book(symbol = USDTUAH)
    #print(str(Ask['asks'][0][0]))
    return Ask['asks'][0][0]

def BUSDUSDT_Bid():
    Bid = client.get_order_book(symbol = BUSDUSDT)
    #print(str(Bid['bids'][0][0]))
    return Bid['bids'][0][0]

def BUSDUSDT_Ask():
    Ask = client.get_order_book(symbol = BUSDUSDT)
    #print(str(Ask['asks'][0][0]))
    return Ask['asks'][0][0]


print("BUSD Bid: " + str(BUSDUAH_Bid()))
print("BUSD Ask: " + str(BUSDUAH_Ask()))
print("USDT Bid: " + str(USDTUAH_Bid()))
print("USDT Ask: " + str(USDTUAH_Ask()))
print("BUSD/USDT Bid: " + str(BUSDUSDT_Bid()))
print("BUSD/USDT Ask: " + str(BUSDUSDT_Ask()))

delta = float(USDTUAH_Ask()) - float(BUSDUAH_Bid())
print("BUSD Bid / USDT Ask Delta: " + str(delta))
if(delta > 0.03):
    print("Buy BUSD and Sell USDT")

delta = float(BUSDUAH_Ask()) - float(USDTUAH_Bid())
print("USDT Bid / BUSD Ask Delta: " + str(delta))
if(delta > 0.03):
    print("Buy USDT and Sell BUSD")

