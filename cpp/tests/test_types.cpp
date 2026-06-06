#include "types.h"
#include <cassert>
#include <cstdint>
#include <string>

int main() {
    polymarket::RawTrade trade{};
    assert(trade.price == 0.0);
    assert(trade.side == polymarket::Side::Unknown);
    assert(trade.market_id == 0);
    assert(trade.asset_id == 0);

    trade.market_id = 42;
    trade.asset_id = 99;
    trade.price = 0.42;
    assert(trade.market_id == 42);
    assert(trade.asset_id == 99);
    assert(trade.price == 0.42);

    polymarket::AnomalyScore score{};
    assert(score.is_large_trade == false);
    assert(score.score == 0);

    score.is_large_trade = true;
    score.score = 3;
    assert(score.is_large_trade == true);
    assert(score.score == 3);

    return 0;
}