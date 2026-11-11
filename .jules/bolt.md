## 2024-05-17 - [Vector Similarity Speedup]
**Learning:** In pure Python semantic search implementations without NumPy/pgvector, using `math.hypot(*vec)` for L2-norm is ~2x faster than a generator expression, and `sum(map(operator.mul, a, b))` is ~1.5x faster than `sum(x * y for x, y in zip(a, b))` for cosine similarity. Since vector search loops over many vectors, these constant factors add up measurably.
**Action:** Use these built-in C-implemented functions for vector arithmetic in pure Python where external libraries aren't used.
## 2026-05-17 - Asynchronous Selenium and Thread Usage
 **Learning:** In an async context, treating synchronous Selenium methods as properties (like `driver.title` or `driver.current_url`) will trigger synchronous HTTP calls and block the event loop if unwrapped.
 **Action:** Instead of `time.sleep()`, use `await asyncio.sleep()`. Convert functions with blocking driver interactions to `async def` and wrap properties with `await asyncio.to_thread(getattr, driver, "title")`.

## $(date +%Y-%m-%d) - Optimizing Sequential Regex Matching
 **Learning:** Sequential `for pattern in patterns: search()` over many regexes is CPU intensive. Combining them into `pattern1|pattern2` pushes the iteration into the faster C-based `re` engine. Adding a fast-path keyword filter (`if any(kw in text)`) avoids regex completely on mismatched strings, yielding ~66% performance improvement for short command phrases.
 **Action:** When matching against multiple known patterns, prefer a pre-compiled combined regex combined with a fast-path keyword check to avoid unnecessary regex evaluation.
