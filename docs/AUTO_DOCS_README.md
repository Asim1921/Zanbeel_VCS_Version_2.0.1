# FoxNest Auto-Docs Feature

Automated documentation generation for FoxNest repositories.

## Features

### Client-Side: `fox docs` Command
Generate documentation for any local project:

```bash
cd /home/aiuser/FoxNest/client
python3 fox.py docs /path/to/project
python3 fox.py docs /path/to/project --lang python
python3 fox.py docs /path/to/project --out /custom/output
python3 fox.py docs /path/to/project --quiet
```

### Client-Side: `fox generate-docs` Command
Generate documentation on the server for your pushed repository:

```bash
# After pushing your repo:
python3 fox.py push
python3 fox.py generate-docs

# With language filter:
python3 fox.py generate-docs --lang python
```

This command:
- Auto-detects your `repo_id` from local config
- Calls the server API to generate docs
- Shows generation results and docs URLs

**Supported Languages:**
- Python (Sphinx-style API + CLI detection)
- JavaScript/TypeScript (JSDoc + React components)
- Java (Javadoc-style)
- C# (XML docs)
- Go (godoc-style)
- Rust (rustdoc-style)
- C/C++ (Doxygen-style)
- PHP, Ruby, Swift, Kotlin, SQL, Dart, Shell, and more

**Output:**
- API reference per language
- CLI reference (with argparse/click/typer detection)
- Dependency documentation
- Project overview
- Coverage metrics

### Server-Side: REST API Endpoints

Generate and serve docs for pushed repositories:

#### 1. Generate Documentation
```bash
POST /api/repository/{repo_id}/generate-docs
```

Optional query params:
- `lang_filter`: Only generate docs for one language (e.g., `python`)

Response:
```json
{
  "success": true,
  "message": "Documentation generated successfully",
  "repo_id": "abc123",
  "docs_url": "/api/repository/abc123/docs",
  "results": {
    "python": {
      "files": 5,
      "classes": 12,
      "functions": 34,
      "documented": 40,
      "coverage": 87.0
    }
  }
}
```

#### 2. List Generated Docs
```bash
GET /api/repository/{repo_id}/docs
```

Response:
```json
{
  "success": true,
  "repo_id": "abc123",
  "files": [
    {
      "path": "index.md",
      "size": 1234,
      "url": "/api/repository/abc123/docs/index.md"
    },
    {
      "path": "api/python/index.md",
      "size": 5678,
      "url": "/api/repository/abc123/docs/api/python/index.md"
    }
  ],
  "metadata": {...},
  "index_url": "/api/repository/abc123/docs/index.md"
}
```

#### 3. Serve Documentation File
```bash
GET /api/repository/{repo_id}/docs/{file_path}
```

Example:
```bash
curl http://localhost:33333/api/repository/abc123/docs/index.md
curl http://localhost:33333/api/repository/abc123/docs/api/python/index.md
```

## Testing

### Typical User Workflow

**Simple flow with client command:**
```bash
# 1. Initialize and push your project
fox init
fox add .
fox commit -m "Initial commit"
fox push

# 2. Generate docs on server
fox generate-docs

# 3. View the docs
curl http://server:33333/api/repository/<repo_id>/docs/index.md
```

**Alternative: Direct API calls:**
```bash
# After pushing, call API directly:
curl -X POST http://server:33333/api/repository/<repo_id>/generate-docs
curl http://server:33333/api/repository/<repo_id>/docs
```

### Automated Test Script
```bash
/home/aiuser/FoxNest/test_auto_docs.sh
```

This script will:
1. Check if server is running
2. Create a test repository with sample Python code
3. Push it to the server
4. Generate documentation via API
5. List and fetch generated docs

### Manual Testing

1. Start the server:
```bash
cd /home/aiuser/FoxNest/server
python3 server.py
```

2. Push a repository with code (or use existing repo)

3. Generate docs via API:
```bash
curl -X POST http://localhost:33333/api/repository/<repo_id>/generate-docs
```

4. View docs list:
```bash
curl http://localhost:33333/api/repository/<repo_id>/docs
```

5. View specific doc file:
```bash
curl http://localhost:33333/api/repository/<repo_id>/docs/index.md
```

## Implementation Details

### Files Added/Modified

**Client:**
- `client/docs_generator.py` - Core documentation generator
- `client/fox.py` - Added `fox docs` subcommand

**Server:**
- `server/docs_generator.py` - Copy of generator for server use
- `server/server.py` - Added 3 new endpoints for docs

### Storage

- Generated docs stored in: `/tmp/foxnest_server/docs/{repo_id}/`
- Each repository gets its own docs directory
- Docs persist until manually cleared or server restart (since using /tmp)

### Documentation Structure

```
docs/
├── index.md                    # Project overview
├── docs_meta.json              # Generation metadata
├── api/
│   ├── python/
│   │   ├── index.md           # Python API index
│   │   └── module_name.md     # Per-module docs
│   └── javascript/
│       ├── index.md           # JS API index
│       └── component_name.md  # Per-file docs
├── cli/
│   └── python_cli.md          # CLI reference
└── dependencies/
    └── index.md               # Dependency list
```

## Future Enhancements

- [ ] HTML output (MkDocs/Sphinx themes)
- [ ] Auto-generate on push (webhook/trigger)
- [ ] Versioned docs (per commit/tag)
- [ ] Search functionality
- [ ] Diagram generation (architecture, call graphs)
- [ ] Code coverage integration
- [ ] Security scan reports
- [ ] Performance metrics
- [ ] Auto-run `--help` and embed CLI output
- [ ] Frontend viewer (React component to browse docs)
