#include "memory_pool.h"
#include <cassert>

int main() {
    polymarket::ObjectPool pool(2);
    auto* a = pool.acquire();
    auto* b = pool.acquire();
    assert(a && b);
    assert(pool.acquire() == nullptr);

    pool.release(a);
    auto* c = pool.acquire();
    assert(c != nullptr);

    return 0;
}