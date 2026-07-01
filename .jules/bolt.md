## 2024-07-01 - Fetch global enumerations concurrently
**Learning:** Found sequential independent I/O HTTP requests (`await client.get(...)`) in `refresh_global_enumerations` fetching Redmine global enumerations (statuses, priorities, roles, activities). This incurred 4x network round-trips.
**Action:** Used `asyncio.gather()` to fetch these requests concurrently. The network block latency changed from $O(n)$ to $O(1)$ roughly reducing time by 75% for 4 requests. Next time, always check if consecutive await calls can be run concurrently.
