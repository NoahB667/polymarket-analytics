#include "memory_pool.h"

namespace polymarket{

    ObjectPool::ObjectPool(std::size_t capacity) 
        : capacity_(capacity), storage_(capacity), available_count_(capacity) {
        // Link all nodes sequentially at startup to maximize hardware prefetcher efficiency
        for (std::size_t i = 0; i < capacity - 1; ++i) {
            storage_[i].next = &storage_[i + 1];
        }

        storage_[capacity - 1].next = nullptr;
        // Point the atomic head to the first node in our block
        head_.store(&storage_[0], std::memory_order_release);
    }

    RawTrade* ObjectPool::acquire() {
        // High-performance lock-free Compare-and-Swap loop
        PoolNode* old_head = head_.load(std::memory_order_acquire);
        while (old_head != nullptr && !head_.compare_exchange_weak(
            old_head, old_head->next, std::memory_order_acq_rel, std::memory_order_acquire)) {}

        if (old_head == nullptr) return nullptr; // Pool is completely exhausted
        available_count_.fetch_sub(1, std::memory_order_relaxed);

        // Clean recycled memory space before handing it to the hot path
        old_head->trade = RawTrade{};
        return &(old_head->trade);
    }

    void ObjectPool::release(RawTrade* item) {
        if (item == nullptr) return;

        // Cast safely by calculating the explicit layout offset (which is 0 here, but compiler-safe)
        auto* node = reinterpret_cast<PoolNode*>(reinterpret_cast<char*>(item) - offsetof(PoolNode, trade));

        // Security boundary check: Ensure the pointer actually belongs inside our pre-allocated memory chunk
        if (node < &storage_.front() || node > &storage_.back()) {
            throw std::runtime_error("Fatal: Attempted to release foreign pointer back to ObjectPool.");
        }

        // Push the node back to the top of our free-list completely lock-free
        PoolNode* old_head = head_.load(std::memory_order_acquire);
        do {
            node->next = old_head;
        } while (!head_.compare_exchange_weak(old_head, node, std::memory_order_acq_rel, std::memory_order_acquire));

        available_count_.fetch_add(1, std::memory_order_relaxed);
    }

    std::size_t ObjectPool::available() const {
        return available_count_.load(std::memory_order_relaxed);
    }
}