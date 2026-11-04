
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from nexus_core.memory.store import Database
from nexus_core.memory.records import MemoryStore
from nexus_core.memory.vector import VectorStore
from nexus_core.recorder import EventRecorder

async def test_hybrid_search():
    # Use an in-memory DB for testing
    db = Database(":memory:")
    db.connect()
    db.migrate()
    
    # Mock recorder
    recorder = AsyncMock(spec=EventRecorder)
    
    # Init MemoryStore (using a temp dir for LanceDB)
    import os
    import shutil
    vector_dir = "/tmp/nexus_test_vectors"
    if os.path.exists(vector_dir):
        shutil.rmtree(vector_dir)
    os.makedirs(vector_dir)
    
    store = MemoryStore(db, vector_dir=vector_dir)
    
    # 1. Add a specific memory
    # Content about "The system's stability is excellent"
    rec = store.create(
        kind="semantic",
        title="System Stability",
        content="The Linux kernel provides a very stable environment for cognitive agents.",
    )
    
    print(f"Created memory: {rec.memory_id}")
    
    # 2. Test Lexical Search (exact word 'stable')
    lex_res = store.search("stable", hybrid=False)
    print(f"Lexical results for 'stable': {len(lex_res)}")
    assert len(lex_res) > 0
    
    # 3. Test Semantic Search (synonym 'reliability' or 'robustness')
    # 'robustness' is semantically close to 'stable'
    sem_res = store.search("reliability of the operating system", hybrid=True)
    print(f"Hybrid results for 'reliability of the operating system': {len(sem_res)}")
    assert len(sem_res) > 0
    assert sem_res[0].memory_id == rec.memory_id
    
    print("Hybrid Search Test PASSED")

if __name__ == "__main__":
    asyncio.run(test_hybrid_search())
