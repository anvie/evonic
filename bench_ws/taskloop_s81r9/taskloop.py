import json
import sys
import os

TASKS_FILE = "tasks.json"

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)

def add_task(description):
    tasks = load_tasks()
    tasks.append({"description": description, "done": False})
    save_tasks(tasks)
    print(f"Added task: {description}")

def list_tasks():
    tasks = load_tasks()
    if not tasks:
        print("No tasks.")
        return
    for i, task in enumerate(tasks, 1):
        status = "[x]" if task["done"] else "[ ]"
        print(f"{i}. {status} {task['description']}")

def done_task(index_str):
    try:
        index = int(index_str)
        tasks = load_tasks()
        if 1 <= index <= len(tasks):
            tasks[index-1]["done"] = True
            save_tasks(tasks)
            print(f"Marked task {index} as done.")
        else:
            print(f"Task {index} not found.")
    except ValueError:
        print("Please provide a valid number.")

def main():
    if len(sys.argv) < 2:
        print("Usage: python taskloop.py [add <desc> | list | done <index>]")
        return

    command = sys.argv[1].lower()
    
    if command == "add" and len(sys.argv) > 2:
        add_task(" ".join(sys.argv[2:]))
    elif command == "list":
        list_tasks()
    elif command == "done" and len(sys.argv) > 2:
        done_task(sys.argv[2])
    elif command == "clear":
        tasks = load_tasks()
        save_tasks([])
        print("Cleared all tasks.")
    else:
        print("Invalid command or missing arguments.")
        print("Usage: python taskloop.py [add <desc> | list | done <index> | clear]")

if __name__ == "__main__":
    main()
