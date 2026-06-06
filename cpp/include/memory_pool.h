#pragma once

#include "types.h"
#include <atomic>
#include <vector>
#include <cstddef>
#include <stdexcept>

namespace polymarket{
    class ObjectPool {
    public:
        // Delete copy constructors to guarantee the pool can never be copied or reallocated in memory
        ObjectPool(const ObjectPool&) = delete;
        ObjectPool& operator=(const ObjectPool&) = delete;
        ObjectPool(ObjectPool&&) noexcept = delete;
        ObjectPool& operator=(ObjectPool&&) noexcept = delete;
        explicit ObjectPool(std::size_t capacity);
        ~ObjectPool() = default;
        RawTrade* acquire();
        void release(RawTrade* item);
        std::size_t available() const;

    private:
        // Node wrapper for an intrusive linked-list layout inside our pre-allocated block
        struct PoolNode {
            RawTrade trade;
            PoolNode* next{nullptr};
        };
        std::size_t capacity_;
        std::vector<PoolNode> storage_;
        // Atomic head pointer for lock-free multi-threaded access
        alignas(64) std::atomic<PoolNode*> head_{nullptr};
        alignas(64) std::atomic<std::size_t> available_count_{0};
    };
}