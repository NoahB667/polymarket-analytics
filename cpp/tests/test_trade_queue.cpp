#include "trade_queue.h"
#include <cassert>

void test_queue_pipeline() {
    polymarket::TradeQueue queue; 

    polymarket::RawTrade trade1;
    trade1.market_id = 42;
    trade1.price = 0.85;

    polymarket::RawTrade trade2;
    trade2.market_id = 99;
    trade2.price = 0.12;

    assert(queue.try_push(trade1));
    assert(queue.try_push(trade2));

    polymarket::RawTrade out;
    
    assert(queue.try_pop(out));
    assert(out.market_id == 42);

    assert(queue.try_pop(out));
    assert(out.market_id == 99);

    assert(!queue.try_pop(out)); // Should be completely empty now
}

int main() {
    test_queue_pipeline();
    return 0;
}