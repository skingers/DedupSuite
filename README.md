# File Deduplicator Suite

A professional, multi-threaded GUI application for identifying, reviewing, and managing duplicate files. It features both exact byte-matching and visual similarity detection for media, wrapped in a modern, elegant, and fast dark-mode interface.

## Features

### Core Functionality
*   **Dual Scanning Engines:**
    *   **Exact Match:** Identifies bit-for-bit duplicates using an efficient two-phase SHA256 hashing process (size -> partial hash -> full hash).
    *   **Visual/Video:** Uses perceptual hashing (pHash) to detect similar images and video frames, robust against resizing or re-encoding.
*   **Folder Merger:** A dedicated tool to merge an "Incoming" directory into a "Master" directory, automatically handling collisions and duplicates.

### Review & Management
*   **Instant Previews:** A highly-optimized, thread-safe image loader provides instant side-by-side previews for images and video frames.
*   **Smart Select:** A heuristic algorithm automatically selects lower-quality or smaller files for deletion, streamlining the review process.
*   **Safety First:** Features a multi-level undo stack for all file operations (delete/move) and a temporary staging area for deleted files, preventing accidental data loss.
*   **Bulk Operations:** Filter results by file type and perform bulk "Delete" or "Move" actions on all visible duplicates.
*   **Find Similar:** In Visual mode, instantly search within your results for other files that are visually similar to the currently selected duplicate.

### User Experience
*   **Modern UI:** Built with `customtkinter` for a clean, elegant, and responsive interface with a consistent dark theme.
*   **Performance:** Fully multi-threaded architecture for scanning, hashing, and preview generation. Thread count is configurable in settings to match your CPU.
*   **Reporting:** Export lists of duplicates to **PDF** or **CSV** for documentation and external analysis.
*   **Advanced Customization:**
    *   Fine-tune the visual similarity threshold.
    *   Define global ignore lists for file extensions and folder names (e.g., `.git`, `cache`).
*   **Cross-Platform:** Built with platform-aware code for Windows, macOS, and Linux file operations.

## Installation

### Prerequisites
*   Python 3.9 or higher

### Option 1: Using pip (Standard)

1.  Clone the repository:
    ```bash
    git clone https://github.com/your-username/dedup-suite.git
    cd dedup-suite
    ```
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

### Option 2: Using uv (Fast)

If you use the high-performance uv package manager:

```bash
uv pip install -r requirements.txt
```

*Note: `reportlab` is optional but required for PDF export functionality.*

## Usage

### Running from Source

Navigate to the directory containing the script and run:

```bash
python dedup_suite.py
```

### Building an Executable

To create a standalone `.exe` for Windows distribution, first install PyInstaller:

```bash
pip install pyinstaller
```

*(Ensure you have an `app.ico` file in the directory, or remove the icon arguments).*

## How to Use

1.  **Audit / Dedup Tab:**
    *   Select a **Source** directory.
    *   Choose **Exact** or **Visual/Video** mode.
    *   Click **Start Scan**.
    *   If **Review Mode** is checked, a window will pop up allowing you to process duplicates.
2.  **Review Window:**
    *   Use **Smart Select** to auto-mark duplicates based on file size.
    *   Use **Move** to relocate duplicates to a specific folder.
    *   Use **Delete Duplicate** to remove the file on the right.
3.  **Merge Folders Tab:**
    *   Select a **Master** (destination) and **Incoming** (source) folder.
    *   Run a **Dry Run** first to see what will happen in the logs.

## Contributing

Contributions are welcome! Please follow these steps:

1.  Fork the repository.
2.  Create a feature branch (`git checkout -b feature/NewFeature`).
3.  Commit your changes and open a Pull Request.