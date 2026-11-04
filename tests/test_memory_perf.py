
import asyncio
import time
import os
import psutil
import numpy as np
from datetime import datetime, timezone
from nexus_core.memory.store import Database
from nexus_core.memory.records import MemoryStore

async def run_performance_test():
    # Setup
    db = Database(":memory:")
    db.connect()
    db.migrate()
    
    # Vector store path
    vector_dir = "/tmp/nexus_perf_vectors"
    if os.path.exists(vector_dir):
        import shutil
        shutil.rmtree(vector_dir)
    os.makedirs(vector_dir)
    
    # Mock recorder
    from unittest.mock import AsyncMock
    recorder = AsyncMock()
    
    store = MemoryStore(db, vector_dir=vector_dir)
    
    # Populate with 500 dummy records to have a realistic scale
    print("Populating memory with 500 records...")
    for i in range(500):
        store.create(
            kind="semantic",
            title=f"Record {i}",
            content=f"This is the content of record {i}. It contains some information about system stability and performance {i}."
        )
    
    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / (1024 * 1024)
    
    queries = ["stable system", "performance metrics", "generic query", "kernel 6.11"]
    
    metrics = {
        "lexical": [],
        "semantic": [],
        "hybrid": []
    }
    
    print("\nMeasuring latencies...")
    for query in queries:
        # Lexical
        start = time.perf_counter()
        store.search(query, hybrid=False)
        metrics["lexical"].append(time.perf_counter() - start)
        
        # Semantic (we can call the internal method for pure semantic measurement)
        start = time.perf_counter()
        store._search_semantic(query, limit=10)
        metrics["semantic"].append(time.perf_counter() - start)
        
        # Hybrid
        start = time.perf_counter()
        store.search(query, hybrid=True)
        metrics["hybrid"].append(time.perf_counter() - start)
    
    mem_after = process.memory_info().rss / (1024 * 1024)
    
    # Disk usage
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(vector_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)

    print("\n--- PERFORMANCE RESULTS ---")
    print(f"Avg Lexical Latency:  {np.mean(metrics['lexical'])*1000:.2f} ms")
    print(f"Avg Semantic Latency: {np.mean(metrics['semantic'])*1000:.2f} ms")
    print(f"Avg Hybrid Latency:   {np.mean(metrics['hybrid'])*1000:.2f} ms")
    print(f"Memory Increase:      {mem_after - mem_before:.2f} MB")
    print(f"Vector Store Size:     {total_size / 1024:.2f} KB")
    print("-" * 25)

if __name__ == "__main__":
    asyncio.run(run_performance_test())
