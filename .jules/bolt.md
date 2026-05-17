## 2024-05-17 - [Vector Similarity Speedup]
**Learning:** In pure Python semantic search implementations without NumPy/pgvector, using `math.hypot(*vec)` for L2-norm is ~2x faster than a generator expression, and `sum(map(operator.mul, a, b))` is ~1.5x faster than `sum(x * y for x, y in zip(a, b))` for cosine similarity. Since vector search loops over many vectors, these constant factors add up measurably.
**Action:** Use these built-in C-implemented functions for vector arithmetic in pure Python where external libraries aren't used.
## 2026-05-17 - Asynchronous Selenium and Thread Usage
 **Learning:** In an async context, treating synchronous Selenium methods as properties (like `driver.title` or `driver.current_url`) will trigger synchronous HTTP calls and block the event loop if unwrapped.
 **Action:** Instead of `time.sleep()`, use `await asyncio.sleep()`. Convert functions with blocking driver interactions to `async def` and wrap properties with `await asyncio.to_thread(getattr, driver, "title")`.
