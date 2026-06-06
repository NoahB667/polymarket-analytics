#include "trade_filter.h"
#include <cassert>

int main() {
    polymarket::RawTrade trade1{};
    trade1.market_id = 1;
    trade1.price = 0.10;
    trade1.usd = 20000;

    polymarket::RawTrade trade2{};
    trade2.market_id = 1;
    trade2.price = 0.70;
    trade2.usd = 1000;

    auto score1 = polymarket::score_trade(trade1);
    auto score2 = polymarket::score_trade(trade2);

    assert(score1.score == 2);
    assert(score1.is_large_trade == true);
    assert(score1.is_long_shot == true);

    assert(score2.score == 0);
    assert(score2.is_large_trade == false);
    assert(score2.is_long_shot == false);
    
    return 0;
}
