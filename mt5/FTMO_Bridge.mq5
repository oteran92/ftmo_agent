//+------------------------------------------------------------------+
//| FTMO_Bridge.mq5                                                  |
//| File-based bridge between Python (FTMO Agent) and MT5            |
//| Writes JSON files to MQL5/Files; reads commands.json for orders  |
//| Symbol can be overridden by bridge_config.json (programmatic)    |
//+------------------------------------------------------------------+
#property copyright "FTMO Agent"
#property version   "1.1"

input string  TradingSymbol    = "EURUSD";  // Symbol to monitor (overridden by bridge_config.json)
input int     WriteIntervalSec = 1;         // Price write interval (seconds)
input bool    EnableTrading    = true;      // Allow order execution via commands.json
input bool    DetailedLogging  = true;      // Print debug logs to Journal

// Active symbol — may be overridden at runtime from bridge_config.json
string ActiveSymbol = "";

datetime last_price   = 0;
datetime last_account = 0;
datetime last_pos     = 0;
datetime last_trades  = 0;
datetime last_h4      = 0;
datetime last_d1      = 0;

//+------------------------------------------------------------------+
//| Read bridge_config.json and return the "symbol" field            |
//| Returns "" if file not found or field missing                    |
//+------------------------------------------------------------------+
string ReadSymbolFromConfig()
{
    int h = FileOpen("bridge_config.json", FILE_READ | FILE_TXT | FILE_ANSI);
    if(h == INVALID_HANDLE) return "";
    string raw = "";
    while(!FileIsEnding(h)) raw += FileReadString(h);
    FileClose(h);
    // Extract "symbol":"VALUE"
    string search = "\"symbol\":\"";
    int s = StringFind(raw, search);
    if(s < 0) return "";
    s += StringLen(search);
    int e = StringFind(raw, "\"", s);
    if(e < 0) return "";
    return StringSubstr(raw, s, e - s);
}

//+------------------------------------------------------------------+
//| Helpers: escape a string for JSON                                |
//+------------------------------------------------------------------+
string JsonStr(string s)
{
    StringReplace(s, "\\", "\\\\");
    StringReplace(s, "\"", "\\\"");
    return s;
}

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
    // Try to read symbol from bridge_config.json (written by Python agent)
    string cfg_sym = ReadSymbolFromConfig();
    if(StringLen(cfg_sym) > 0)
    {
        ActiveSymbol = cfg_sym;
        Print("FTMO_Bridge: symbol loaded from bridge_config.json -> ", ActiveSymbol);
    }
    else
    {
        // Fallback to the UI input parameter
        ActiveSymbol = TradingSymbol;
        Print("FTMO_Bridge: using input parameter symbol -> ", ActiveSymbol);
    }
    Print("FTMO_Bridge started. Symbol=", ActiveSymbol, " Trading=", EnableTrading);
    EventSetTimer(1);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    Print("FTMO_Bridge stopped.");
}

//+------------------------------------------------------------------+
//| OnTimer — fires every second                                     |
//+------------------------------------------------------------------+
void OnTimer()
{
    datetime now = TimeCurrent();

    if(now - last_price >= WriteIntervalSec)
    {
        WritePriceData();
        last_price = now;
    }
    if(now - last_account >= 2)
    {
        WriteAccountInfo();
        last_account = now;
    }
    if(now - last_pos >= 2)
    {
        WritePositions();
        last_pos = now;
    }
    if(now - last_trades >= 15)
    {
        WriteClosedTrades();
        last_trades = now;
    }
    // Write H4 candles every 5 minutes
    if(now - last_h4 >= 300)
    {
        WriteCandles(ActiveSymbol, PERIOD_H4, 100, ActiveSymbol + "_rates_H4.json");
        last_h4 = now;
    }
    // Write D1 candles every 10 minutes
    if(now - last_d1 >= 600)
    {
        WriteCandles(ActiveSymbol, PERIOD_D1, 60, ActiveSymbol + "_rates_D1.json");
        last_d1 = now;
    }
    if(EnableTrading && MQLInfoInteger(MQL_TRADE_ALLOWED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
    {
        CheckTradeCommands();
    }
}

//+------------------------------------------------------------------+
//| Write OHLC candles for a given timeframe                         |
//+------------------------------------------------------------------+
void WriteCandles(string sym, ENUM_TIMEFRAMES tf, int count, string filename)
{
    MqlRates rates[];
    int copied = CopyRates(sym, tf, 0, count, rates);
    if(copied <= 0) return;

    string tf_name = "";
    if(tf == PERIOD_H4) tf_name = "H4";
    else if(tf == PERIOD_D1) tf_name = "D1";

    string json = "{";
    json += "\"symbol\":\"" + sym + "\",";
    json += "\"timeframe\":\"" + tf_name + "\",";
    json += "\"count\":" + IntegerToString(copied) + ",";
    json += "\"candles\":[";

    for(int i = copied - 1; i >= 0; i--)
    {
        if(i != copied - 1) json += ",";
        json += "{";
        json += "\"t\":"    + IntegerToString((long)rates[i].time) + ",";
        json += "\"time\":\"" + TimeToString(rates[i].time) + "\",";
        json += "\"o\":"    + DoubleToString(rates[i].open,  5) + ",";
        json += "\"h\":"    + DoubleToString(rates[i].high,  5) + ",";
        json += "\"l\":"    + DoubleToString(rates[i].low,   5) + ",";
        json += "\"c\":"    + DoubleToString(rates[i].close, 5) + ",";
        json += "\"vol\":"  + IntegerToString((long)rates[i].tick_volume);
        json += "}";
    }

    json += "]}";

    int h = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(h != INVALID_HANDLE) { FileWrite(h, json); FileClose(h); }
}

//+------------------------------------------------------------------+
//| Write bid/ask snapshot for the configured symbol                 |
//+------------------------------------------------------------------+
void WritePriceData()
{
    double bid = SymbolInfoDouble(ActiveSymbol, SYMBOL_BID);
    double ask = SymbolInfoDouble(ActiveSymbol, SYMBOL_ASK);
    datetime t = TimeCurrent();

    string json = "{";
    json += "\"symbol\":\""  + JsonStr(ActiveSymbol) + "\",";
    json += "\"bid\":"       + DoubleToString(bid, 5) + ",";
    json += "\"ask\":"       + DoubleToString(ask, 5) + ",";
    json += "\"spread\":"    + DoubleToString(ask - bid, 5) + ",";
    json += "\"timestamp\":" + IntegerToString(t) + ",";
    json += "\"server_time\":\"" + TimeToString(t) + "\"";
    json += "}";

    string fname = ActiveSymbol + "_price.json";
    int h = FileOpen(fname, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(h != INVALID_HANDLE) { FileWrite(h, json); FileClose(h); }
}

//+------------------------------------------------------------------+
//| Write live account information                                   |
//+------------------------------------------------------------------+
void WriteAccountInfo()
{
    double  balance     = AccountInfoDouble(ACCOUNT_BALANCE);
    double  equity      = AccountInfoDouble(ACCOUNT_EQUITY);
    double  margin      = AccountInfoDouble(ACCOUNT_MARGIN);
    double  free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    double  profit      = AccountInfoDouble(ACCOUNT_PROFIT);
    double  credit      = AccountInfoDouble(ACCOUNT_CREDIT);
    string  currency    = AccountInfoString(ACCOUNT_CURRENCY);
    string  name        = AccountInfoString(ACCOUNT_NAME);
    string  server      = AccountInfoString(ACCOUNT_SERVER);
    string  company     = AccountInfoString(ACCOUNT_COMPANY);
    datetime t          = TimeCurrent();

    long login_val = AccountInfoInteger(ACCOUNT_LOGIN);
    long lev_val   = AccountInfoInteger(ACCOUNT_LEVERAGE);
    long mode_val  = AccountInfoInteger(ACCOUNT_TRADE_MODE);

    bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
    long ping_val  = TerminalInfoInteger(TERMINAL_PING_LAST);
    long build_val = TerminalInfoInteger(TERMINAL_BUILD);

    string json = "{";
    json += "\"login\":"     + IntegerToString(login_val) + ",";
    json += "\"name\":\""    + JsonStr(name)    + "\",";
    json += "\"server\":\""  + JsonStr(server)  + "\",";
    json += "\"company\":\"" + JsonStr(company) + "\",";
    json += "\"currency\":\"" + JsonStr(currency) + "\",";
    json += "\"leverage\":"  + IntegerToString((int)lev_val)  + ",";
    json += "\"trade_mode\":" + IntegerToString((int)mode_val) + ",";
    json += "\"balance\":"    + DoubleToString(balance, 2)    + ",";
    json += "\"equity\":"     + DoubleToString(equity, 2)     + ",";
    json += "\"margin\":"     + DoubleToString(margin, 2)     + ",";
    json += "\"free_margin\":" + DoubleToString(free_margin, 2) + ",";
    json += "\"profit\":"     + DoubleToString(profit, 2)     + ",";
    json += "\"credit\":"     + DoubleToString(credit, 2)     + ",";
    json += "\"terminal_connected\":" + (connected ? "true" : "false") + ",";
    json += "\"terminal_ping_ms\":"   + IntegerToString((int)ping_val)  + ",";
    json += "\"terminal_build\":"     + IntegerToString((int)build_val) + ",";
    json += "\"timestamp\":"   + IntegerToString(t) + ",";
    json += "\"server_time\":\"" + TimeToString(t) + "\"";
    json += "}";

    int h = FileOpen("account_info.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(h != INVALID_HANDLE) { FileWrite(h, json); FileClose(h); }
}

//+------------------------------------------------------------------+
//| Write all open positions                                         |
//+------------------------------------------------------------------+
void WritePositions()
{
    int total = PositionsTotal();
    string json = "{";
    json += "\"timestamp\":" + IntegerToString(TimeCurrent()) + ",";
    json += "\"positions\":[";

    int count = 0;
    for(int i = 0; i < total; i++)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket <= 0 || !PositionSelectByTicket(ticket)) continue;

        string sym        = PositionGetString(POSITION_SYMBOL);
        long   ptype      = PositionGetInteger(POSITION_TYPE);
        double vol        = PositionGetDouble(POSITION_VOLUME);
        double p_open     = PositionGetDouble(POSITION_PRICE_OPEN);
        double p_cur      = PositionGetDouble(POSITION_PRICE_CURRENT);
        double sl         = PositionGetDouble(POSITION_SL);
        double tp         = PositionGetDouble(POSITION_TP);
        double pos_profit = PositionGetDouble(POSITION_PROFIT);
        double swap       = PositionGetDouble(POSITION_SWAP);
        string comment    = PositionGetString(POSITION_COMMENT);
        long   magic      = PositionGetInteger(POSITION_MAGIC);
        long   topen      = PositionGetInteger(POSITION_TIME);

        if(count > 0) json += ",";
        json += "{";
        json += "\"ticket\":"       + IntegerToString((long)ticket) + ",";
        json += "\"symbol\":\""     + JsonStr(sym) + "\",";
        json += "\"type\":\""       + (ptype == POSITION_TYPE_BUY ? "buy" : "sell") + "\",";
        json += "\"volume\":"       + DoubleToString(vol, 2) + ",";
        json += "\"price_open\":"   + DoubleToString(p_open, 5) + ",";
        json += "\"price_current\":" + DoubleToString(p_cur, 5) + ",";
        json += "\"sl\":"           + DoubleToString(sl, 5) + ",";
        json += "\"tp\":"           + DoubleToString(tp, 5) + ",";
        json += "\"profit\":"       + DoubleToString(pos_profit, 2) + ",";
        json += "\"swap\":"         + DoubleToString(swap, 2) + ",";
        json += "\"magic\":"        + IntegerToString((int)magic) + ",";
        json += "\"comment\":\""    + JsonStr(comment) + "\",";
        json += "\"time_open\":\""  + TimeToString((datetime)topen) + "\",";
        json += "\"time_open_ts\":"  + IntegerToString(topen);
        json += "}";
        count++;
    }

    json += "]}";

    int h = FileOpen("positions.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(h != INVALID_HANDLE) { FileWrite(h, json); FileClose(h); }
    if(DetailedLogging && count > 0) Print("Positions updated: ", count);
}

//+------------------------------------------------------------------+
//| Write last 30 days of closed trades (deal exits)                 |
//+------------------------------------------------------------------+
void WriteClosedTrades()
{
    datetime from = TimeCurrent() - (30 * 86400);
    if(!HistorySelect(from, TimeCurrent())) return;

    int total = HistoryDealsTotal();
    string json = "{";
    json += "\"timestamp\":" + IntegerToString(TimeCurrent()) + ",";
    json += "\"trades\":[";

    int count = 0;
    for(int i = total - 1; i >= 0; i--)
    {
        ulong dticket = HistoryDealGetTicket(i);
        if(dticket <= 0) continue;
        long entry = HistoryDealGetInteger(dticket, DEAL_ENTRY);
        if(entry != DEAL_ENTRY_OUT) continue;

        string dsym    = HistoryDealGetString(dticket, DEAL_SYMBOL);
        long   dtype   = HistoryDealGetInteger(dticket, DEAL_TYPE);
        double dvol    = HistoryDealGetDouble(dticket, DEAL_VOLUME);
        double dprice  = HistoryDealGetDouble(dticket, DEAL_PRICE);
        double dprofit = HistoryDealGetDouble(dticket, DEAL_PROFIT);
        double dswap   = HistoryDealGetDouble(dticket, DEAL_SWAP);
        double dcomm   = HistoryDealGetDouble(dticket, DEAL_COMMISSION);
        long   dtime   = HistoryDealGetInteger(dticket, DEAL_TIME);
        string dcomment = HistoryDealGetString(dticket, DEAL_COMMENT);

        if(count > 0) json += ",";
        json += "{";
        json += "\"ticket\":"     + IntegerToString((long)dticket) + ",";
        json += "\"symbol\":\""   + JsonStr(dsym) + "\",";
        json += "\"type\":\""     + (dtype == DEAL_TYPE_BUY ? "buy" : "sell") + "\",";
        json += "\"volume\":"     + DoubleToString(dvol, 2) + ",";
        json += "\"price\":"      + DoubleToString(dprice, 5) + ",";
        json += "\"profit\":"     + DoubleToString(dprofit + dswap + dcomm, 2) + ",";
        json += "\"close_time\":\"" + TimeToString((datetime)dtime) + "\",";
        json += "\"close_ts\":"   + IntegerToString(dtime) + ",";
        json += "\"comment\":\""  + JsonStr(dcomment) + "\"";
        json += "}";
        count++;
        if(count >= 200) break; // cap at 200 trades
    }

    json += "]}";
    int h = FileOpen("closed_trades.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(h != INVALID_HANDLE) { FileWrite(h, json); FileClose(h); }
    if(DetailedLogging) Print("Closed trades exported: ", count);
}

//+------------------------------------------------------------------+
//| JSON extraction helpers                                          |
//+------------------------------------------------------------------+
string ExtractStr(string json, string key)
{
    string search = "\"" + key + "\":\"";
    int s = StringFind(json, search);
    if(s < 0) return "";
    s += StringLen(search);
    int e = StringFind(json, "\"", s);
    if(e < 0) return "";
    return StringSubstr(json, s, e - s);
}

double ExtractDbl(string json, string key)
{
    string search = "\"" + key + "\":";
    int s = StringFind(json, search);
    if(s < 0) return 0;
    s += StringLen(search);
    int e = StringFind(json, ",", s);
    int e2 = StringFind(json, "}", s);
    if(e < 0 || (e2 >= 0 && e2 < e)) e = e2;
    if(e < 0) return 0;
    return StringToDouble(StringSubstr(json, s, e - s));
}

long ExtractLng(string json, string key)
{
    string search = "\"" + key + "\":";
    int s = StringFind(json, search);
    if(s < 0) return 0;
    s += StringLen(search);
    int e = StringFind(json, ",", s);
    int e2 = StringFind(json, "}", s);
    if(e < 0 || (e2 >= 0 && e2 < e)) e = e2;
    if(e < 0) return 0;
    return StringToInteger(StringSubstr(json, s, e - s));
}

//+------------------------------------------------------------------+
//| Read commands.json and execute the trade action                  |
//+------------------------------------------------------------------+
void CheckTradeCommands()
{
    int h = FileOpen("commands.json", FILE_READ | FILE_TXT | FILE_ANSI);
    if(h == INVALID_HANDLE) return;

    string cmd = "";
    while(!FileIsEnding(h)) cmd += FileReadString(h);
    FileClose(h);

    if(StringLen(cmd) < 5) return;

    string action = ExtractStr(cmd, "action");
    if(action == "") return;

    if(DetailedLogging) Print("Command received: ", cmd);

    bool ok = false;
    string trade_id = ExtractStr(cmd, "trade_id");

    if(action == "buy" || action == "sell")
    {
        string sym   = ExtractStr(cmd, "symbol");
        if(sym == "") sym = ActiveSymbol;
        double lots  = ExtractDbl(cmd, "lot_size");
        if(lots <= 0) lots = 0.01;
        double sl    = ExtractDbl(cmd, "stop_loss");
        double tp    = ExtractDbl(cmd, "take_profit");
        string cmt   = ExtractStr(cmd, "comment");
        long   magic = ExtractLng(cmd, "magic_number");

        MqlTradeRequest req; ZeroMemory(req);
        MqlTradeResult  res; ZeroMemory(res);

        req.action       = TRADE_ACTION_DEAL;
        req.symbol       = sym;
        req.volume       = NormalizeDouble(lots, 2);
        req.type         = (action == "buy") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
        req.price        = (action == "buy")
                           ? SymbolInfoDouble(sym, SYMBOL_ASK)
                           : SymbolInfoDouble(sym, SYMBOL_BID);
        req.sl           = sl;
        req.tp           = tp;
        req.deviation    = 50;
        req.magic        = (ulong)magic;
        req.comment      = cmt;
        req.type_filling = ORDER_FILLING_IOC;

        ok = OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE;
        Print("Order ", action, " result: ", res.retcode, " deal=", res.deal);
    }
    else if(action == "close")
    {
        long ticket = ExtractLng(cmd, "ticket");
        double cvol = ExtractDbl(cmd, "close_volume");
        if(ticket > 0 && PositionSelectByTicket((ulong)ticket))
        {
            string sym  = PositionGetString(POSITION_SYMBOL);
            long   ptype = PositionGetInteger(POSITION_TYPE);
            double pvol = PositionGetDouble(POSITION_VOLUME);
            double vol  = (cvol > 0 && cvol < pvol) ? cvol : pvol;

            MqlTradeRequest req; ZeroMemory(req);
            MqlTradeResult  res; ZeroMemory(res);
            req.action       = TRADE_ACTION_DEAL;
            req.position     = (ulong)ticket;
            req.symbol       = sym;
            req.volume       = NormalizeDouble(vol, 2);
            req.type         = (ptype == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
            req.price        = (ptype == POSITION_TYPE_BUY)
                               ? SymbolInfoDouble(sym, SYMBOL_BID)
                               : SymbolInfoDouble(sym, SYMBOL_ASK);
            req.deviation    = 50;
            req.type_filling = ORDER_FILLING_IOC;
            req.comment      = "FTMO-Agent close";

            ok = OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE;
            Print("Close result: ", res.retcode);
        }
    }
    else if(action == "modify")
    {
        long ticket = ExtractLng(cmd, "ticket");
        double sl   = ExtractDbl(cmd, "stop_loss");
        double tp   = ExtractDbl(cmd, "take_profit");
        if(ticket > 0 && PositionSelectByTicket((ulong)ticket))
        {
            string sym = PositionGetString(POSITION_SYMBOL);
            MqlTradeRequest req; ZeroMemory(req);
            MqlTradeResult  res; ZeroMemory(res);
            req.action   = TRADE_ACTION_SLTP;
            req.position = (ulong)ticket;
            req.symbol   = sym;
            req.sl       = (sl > 0) ? sl : PositionGetDouble(POSITION_SL);
            req.tp       = (tp > 0) ? tp : PositionGetDouble(POSITION_TP);
            ok = OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE;
            Print("Modify result: ", res.retcode);
        }
    }

    // Log result to trade_results.txt
    int lh = FileOpen("trade_results.txt", FILE_WRITE | FILE_READ | FILE_TXT | FILE_ANSI);
    if(lh != INVALID_HANDLE)
    {
        FileSeek(lh, 0, SEEK_END);
        FileWrite(lh, TimeToString(TimeCurrent()) + " | " + action
                      + " | " + (ok ? "SUCCESS" : "FAIL")
                      + " | id:" + trade_id);
        FileClose(lh);
    }

    // Clear command so EA does not re-execute
    int ch = FileOpen("commands.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(ch != INVALID_HANDLE) { FileWrite(ch, "{}"); FileClose(ch); }
}
