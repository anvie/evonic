import sys
import json
import os

# Get the directory where the script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(SCRIPT_DIR, "tasks.json")

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
        json.dump(tasks, f, indent=4)

def add_task(description):
    tasks = load_tasks()
    tasks.append({"description": description, "done": False})
    save_tasks(tasks)
    print(f"Added task: {description}")

def list_tasks():
    tasks = load_tasks()
    if not tasks:
        print("No tasks found.")
        return
    for i, task in enumerate(tasks):
        status = "[92mDone[0m]" if task.get("done") else "[ ]"
        description = task.get("description", "No description")
        print(f"{i + 1}. {status} {description}")

def done_task(index):
    tasks = load_tasks()
    try:
        tasks[index]["done"] = True
        save_tasks(tasks)
        print(f"Marked task {index + 1} as done.")
    except IndexError:
        print("Invalid task number.")

def clear_tasks():
    save_tasks([])
    print("All tasks cleared.")

def main():
    if len(sys.argv) < 2:
        print("Usage: python taskloop.py [add|list|done|clear] [args]")
        return

    command = sys.argv[1].lower()

    if command == "add" and len(sys.argv) > 2:
        add_task(" ".join(sys.argv[2:]))
    elif command == "list":
        list_tasks()
    elif command == "done" and len(sys.argv) > 2:
        try:
            idx = int(sys.argv[2]) - 1
            done_task(idx)
        except ValueError:
            print("Please provide a valid task number.")
    elif command == "clear":
        clear_tasks()
    else:
        print("Invalid command or missing arguments.")

if __name__ == "__main__":
    main()
