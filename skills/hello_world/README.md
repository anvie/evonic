# Hello World Skill

## Overview

**Hello World Skill** adalah skill dasar yang menyediakan kemampuan untuk menghasilkan pesan sapaan personalisasi berdasarkan nama yang diberikan. Skill ini merupakan contoh minimal dari arsitektur skill di Evonic — menunjukkan bagaimana skill didefinisikan, diimplementasikan, dan dipanggil oleh agent.

## Struktur File

```
hello_world/
├── skill.json                    # Metadata skill
├── SYSTEM.md                     # System prompt untuk agent
├── tools.json                    # Definisi tool (function schema)
├── setup.py                      # Skrip install/uninstall
└── backend/
    └── tools/
        ├── __init__.py
        └── hello_world.py        # Implementasi tool
```

## Cara Kerja

### 1. Metadata (`skill.json`)

File ini mendefinisikan identitas dan konfigurasi skill:

```json
{
  "id": "hello_world",
  "name": "Hello World",
  "version": "1.0.0",
  "description": "A simple greeting skill that says hello to anyone.",
  "author": "Evonic",
  "tools_file": "tools.json",
  "enabled": true,
  "variables": []
}
```

- **`id`**: Identifier unik skill — digunakan saat memanggil skill.
- **`tools_file`**: Referensi ke file yang berisi definisi tool yang tersedia di skill ini.
- **`enabled`**: Flag untuk mengaktifkan/menonaktifkan skill.

### 2. Sistem Prompt (`SYSTEM.md`)

File ini berisi instruksi yang dimuat ke dalam konteks agent saat skill di-load. Isinya menjelaskan:

- **Tujuan skill**: Menghasilkan pesan sapaan personalisasi.
- **Cara penggunaan**: Agent harus memanggil tool `hello_world` saat user meminta untuk menyapa seseorang.
- **Aturan khusus**: Setiap kali ingin menjalankan skill, input string harus dikonkatenasi dengan `" WAWA!"`.
- **Contoh output**: JSON terstruktur dengan `status` dan `message`.

### 3. Definisi Tool (`tools.json`)

File ini mendefinisikan schema tool yang bisa dipanggil oleh agent. Berisi fungsi dengan parameter:

```json
{
  "function": {
    "description": "Say hello to someone by name. Returns a personalized greeting message.",
    "name": "hello_world",
    "parameters": {
      "properties": {
        "name": {
          "description": "Name of the person to greet",
          "type": "string"
        }
      },
      "required": ["name"],
      "type": "object"
    }
  },
  "type": "function"
}
```

Agent membaca schema ini untuk mengetahui:
- Nama tool: `hello_world`
- Parameter yang dibutuhkan: `name` (string, wajib)
- Deskripsi tool

### 4. Implementasi Tool (`backend/tools/hello_world.py`)

Ini adalah logika inti yang dieksekusi saat tool dipanggil:

```python
def execute(agent: dict, args: dict) -> dict:
    name = args.get("name", "Guest")
    greeting = f"Hello, {name}! Welcome to the Hello World skill. 👋"
    return {
        "status": "success",
        "message": greeting,
        "greeting": greeting
    }
```

**Alur eksekusi:**
1. Fungsi `execute()` dipanggil oleh skill manager.
2. Parameter `name` diambil dari `args`. Jika tidak ada, default ke `"Guest"`.
3. Pesan sapaan dibentuk menggunakan f-string.
4. Response JSON dikembalikan dengan `status`, `message`, dan `greeting`.

**Parameter fungsi:**
- **`agent`**: Dictionary konteks agent (termasuk metadata session, user, dll).
- **`args`**: Dictionary argumen yang diteruskan dari agent (berisi `name`).

**Mengembalikan gambar (opsional):** tool boleh menambahkan `images` ke hasil dict, misalnya preview render, grafik atau screenshot:

```python
return {
    "status": "success",
    "message": "Preview siap",
    "images": [{"mime": "image/png", "data": "<base64>", "label": "preview"}],
}
```

Setiap gambar (png, jpeg atau webp, maks. 4 MB, maks. 4 per hasil) disimpan sebagai attachment sesi, dan di hasil tool diganti dengan referensi (`tool_image_attachment_id`, `label`, `path`), jadi transcript tetap berupa teks. Bila model **Vision Supported** dan vision agent aktif, model langsung melihat gambar tersebut pada panggilan berikutnya; selain itu model bisa membacanya dengan `describe_image` memakai `path` tadi.

### 5. Skrip Install/Uninstall (`setup.py`)

Dipanggil oleh skills manager saat skill di-install atau di-uninstall:

- **`install(context)`**: Dipanggil saat skill di-install/re-install.
- **`uninstall(context)`**: Dipanggil saat skill di-remove.

**`context` dict berisi:**
- `skill_dir`: Path absolut ke direktori skill.
- `app_dir`: Path absolut ke direktori aplikasi utama.
- `skill_id`: ID string skill.

## Aturan Penggunaan

1. **Konkatenasi " WAWA!"**: Setiap kali ingin menjalankan skill `hello_world`, input string harus dikonkatenasi dengan `" WAWA!"` terlebih dahulu.
2. **Gunakan output langsung**: Jangan memodifikasi teks sapaan dari output tool — gunakan apa adanya.
3. **Parameter wajib**: `name` harus selalu disediakan.

## Catatan

- Skill ini bersifat **stateless** — tidak menyimpan data antar pemanggilan.
- Tool selalu mengembalikan response dengan `status: "success"`.
- Emoji 👋 ditambahkan secara otomatis di akhir pesan sapaan.
