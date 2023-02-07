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

#logging
log = open("log.txt", "a")

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

def BUYBUSD():
    order = client.order_market_buy(symbol=BUSDUAH, quantity=100)
    log.write(str(order + "\n"))
    order = client.order_market_sell(symbol=BUSDUSDT, quantity=100)
    log.write(str(order + "\n"))
    order = client.order_market_sell(symbol=USDTUAH, quantity=100)
    log.write(str(order + "\n"))

def BUYUSDT():
    order = client.order_market_buy(symbol=USDTUAH, quantity=100)
    log.write(str(order + "\n"))
    order = client.order_market_buy(symbol=BUSDUSDT, quantity=100)
    log.write(str(order + "\n"))
    order = client.order_market_sell(symbol=BUSDUAH, quantity=100)
    log.write(str(order + "\n"))

while(key.loop == 'false'):
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
        while():
            print("Executing stuff!!")
            #BUYBUSD()
            delta = float(USDTUAH_Ask()) - float(BUSDUAH_Bid())
        delta = 0

    delta = float(BUSDUAH_Ask()) - float(USDTUAH_Bid())
    print("USDT Bid / BUSD Ask Delta: " + str(delta))
    if(delta > 0.03):
        print("Buy USDT and Sell BUSD")
        while(delta > 0.03):
            print("Executing!!")
            #BUYUSDT()
            delta = float(BUSDUAH_Ask()) - float(USDTUAH_Bid())
        delta = 0
