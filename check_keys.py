import json; tasks = json.load(open('bench_ws/taskloop_s72r4/tasks.json')); for i, t in enumerate(tasks): print(f'Item {i} keys: {t.keys()}')
