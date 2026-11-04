
import asyncio
import numpy as np
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock
from nexus_core.memory.store import Database
from nexus_core.memory.records import MemoryStore
from nexus_core.recorder import EventRecorder

async def run_benchmark():
    # Setup
    db = Database(":memory:")
    db.connect()
    db.migrate()
    
    recorder = AsyncMock(spec=EventRecorder)
    import os, shutil
    vector_dir = "/tmp/nexus_benchmark_vectors"
    if os.path.exists(vector_dir): shutil.rmtree(vector_dir)
    os.makedirs(vector_dir)
    
    store = MemoryStore(db, vector_dir=vector_dir)

    # Golden Samples: (title, content, query, expected_found)
    samples = [
        # Scenario 1: Lexical match (Exact technical term)
        ("Kernel Version", "The current kernel is 6.11.0-generic", "6.11.0-generic", True),
        # Scenario 2: Semantic match (Concept: Stability)
        ("System Health", "The OS exhibits high stability and low crash rates", "Is the system reliable?", True),
        # Scenario 3: Semantic match (Concept: Performance)
        ("RAM Usage", "Memory consumption is optimized for low-end hardware", "How is the resource efficiency?", True),
        # Scenario 4: False Positive (Common word, different meaning)
        ("Apple Fruit", "The apple is a sweet red fruit", "The system apple core", False),
        # Scenario 5: Hybrid (Both match)
        ("Network Config", "The network is configured for loopback only", "network loopback", True),
    ]

    # Populate
    for title, content, _, _ in samples:
        store.create(kind="semantic", title=title, content=content)

    print(f"{'Query':<40} | {'Lex':<5} | {'Sem':<5} | {'Hyb':<5}")
    print("-" * 65)

    results = {"lex": 0, "sem": 0, "hyb": 0}

    for title, content, query, expected in samples:
        # Lexical
        res_lex = store.search(query, hybrid=False)
        found_lex = any(r.content == content for r in res_lex)
        
        # Semantic (we simulate this by setting hybrid=True but the la lancia only semantic usually? 
        # No, currently our 'search' with hybrid=False is lexical. 
        # To test purely semantic, we need to check if the la lancia can do it. 
        # Actually, we can just check the results of hybrid vs lexical)
        
        # Let's use the internal methods for pure testing
        res_sem = store._search_semantic(query, limit=5)
        found_sem = any(r.content == content for r in res_sem)
        
        # Hybrid
        res_hyb = store.search(query, hybrid=True)
        found_hyb = any(r.content == content for r in res_hyb)

        if found_lex: results["lex"] += 1
        if found_sem: results["sem"] += 1
        if found_hyb: results["hyb"] += 1

        print(f"{query:<40} | {str(found_lex):<5} | {str(found_sem):<5} | {str(found_hyb):<5}")

    print("-" * 65)
    print(f"TOTALS: Lexical: {results['lex']}/{len(samples)} | Semantic: {results['sem']}/{len(samples)} | Hybrid: {results['hyb']}/{len(samples)}")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
