import json
import sys
import os

TASKS_FILE = 'tasks.json'

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_tasks(tasks):
    with open(TASKS_FILE, 'w') as f:
        json.dump(tasks, f, indent=2)

def add_task(content=None):
    if content is None:
        content = input("Enter task: ")
    tasks = load_tasks()
    tasks.append({"id": len(tasks) + 1, "content": content, "done": False})
    save_tasks(tasks)
    print(f"Added task {len(tasks)}.")

def list_tasks():
    tasks = load_tasks()
    if not tasks:
        print("No tasks found.")
        return
    for i, task in enumerate(tasks):
        status = "[X]" if task["done"] else "[ ]"
        print(f"{i + 1}. {status} {task['content']}")

def done_task(idx_str=None):
    tasks = load_tasks()
    if not tasks:
        print("No tasks found.")
        return
    try:
        idx = int(idx_str) if idx_str else int(input("Enter task number to mark as done: ")) - 1
        if 0 <= idx < len(tasks):
            tasks[idx]["done"] = True
            save_tasks(tasks)
            print(f"Marked task {idx + 1} as done.")
        else:
            print("Invalid task number.")
    except ValueError:
        print("Please enter a valid number.")

def clear_tasks():
    save_tasks([])
    print("All tasks cleared.")

def main():
    if len(sys.argv) < 2:
        print("Usage: python taskloop.py [add|list|done] [content/index]")
        return

    command = sys.argv[1].lower()
    if command == "add":
        add_task(sys.argv[2] if len(sys.argv) > 2 else None)
    elif command == "list":
        list_tasks()
    elif command == "done":
        done_task(sys.argv[2] if len(sys.argv) > 2 else None)
    elif command == "clear":
        clear_tasks()
    else:
        print("Unknown command. Use add, list, done, or clear.")

if __name__ == "__main__":
    main()
