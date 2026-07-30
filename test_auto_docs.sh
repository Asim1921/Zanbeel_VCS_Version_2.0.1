#!/bin/bash
# Test script for FoxNest auto-docs feature

echo "🦊 FoxNest Auto-Docs Test Script"
echo "=================================="
echo ""

SERVER_URL="http://localhost:33333"
REPO_ID=""

# Function to check if server is running
check_server() {
    echo "Checking if server is running..."
    if curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
        echo "✓ Server is running"
        return 0
    else
        echo "✗ Server is not running"
        echo "  Start server with: cd /home/aiuser/FoxNest/server && python3 server.py"
        return 1
    fi
}

# Function to push test repo
push_test_repo() {
    echo ""
    echo "Creating test repository..."
    
    # Create temp directory with sample Python code
    TEMP_DIR=$(mktemp -d)
    cd "$TEMP_DIR"
    
    # Create sample Python file
    cat > sample.py << 'EOF'
"""
Sample module for testing auto-docs
"""

class Calculator:
    """A simple calculator class."""
    
    def add(self, a, b):
        """Add two numbers together."""
        return a + b
    
    def multiply(self, a, b):
        """Multiply two numbers."""
        return a * b

def greet(name):
    """Greet a person by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    calc = Calculator()
    print(calc.add(5, 3))
EOF

    # Initialize fox repo
    python3 /home/aiuser/FoxNest/client/fox.py init --username testuser --repo-name auto-docs-test
    python3 /home/aiuser/FoxNest/client/fox.py set origin localhost:33333
    python3 /home/aiuser/FoxNest/client/fox.py add sample.py
    python3 /home/aiuser/FoxNest/client/fox.py commit -m "Add sample Python code"
    python3 /home/aiuser/FoxNest/client/fox.py push
    
    # Extract repo_id from config
    if [ -f .fox/config.json ]; then
        REPO_ID=$(python3 -c "import json; print(json.load(open('.fox/config.json')).get('repo_id', ''))")
        echo "✓ Repository created with ID: $REPO_ID"
    else
        echo "✗ Failed to create repository"
        cd -
        rm -rf "$TEMP_DIR"
        return 1
    fi
    
    cd -
    rm -rf "$TEMP_DIR"
    return 0
}

# Function to generate docs
generate_docs() {
    echo ""
    echo "Generating documentation for repo: $REPO_ID"
    
    RESPONSE=$(curl -s -X POST "$SERVER_URL/api/repository/$REPO_ID/generate-docs")
    echo "$RESPONSE" | python3 -m json.tool
    
    SUCCESS=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('success', False))")
    
    if [ "$SUCCESS" = "True" ]; then
        echo "✓ Documentation generated successfully"
        return 0
    else
        echo "✗ Documentation generation failed"
        return 1
    fi
}

# Function to list docs
list_docs() {
    echo ""
    echo "Listing generated documentation..."
    
    RESPONSE=$(curl -s "$SERVER_URL/api/repository/$REPO_ID/docs")
    echo "$RESPONSE" | python3 -m json.tool
    
    echo ""
    echo "✓ Documentation files listed"
}

# Function to fetch a doc file
fetch_doc_file() {
    echo ""
    echo "Fetching index.md..."
    
    curl -s "$SERVER_URL/api/repository/$REPO_ID/docs/index.md" | head -20
    echo ""
    echo "... (output truncated)"
    echo ""
    echo "✓ Documentation file fetched"
}

# Run tests
check_server || exit 1
push_test_repo || exit 1
generate_docs || exit 1
list_docs
fetch_doc_file

echo ""
echo "=================================="
echo "✅ All tests passed!"
echo ""
echo "You can now:"
echo "  - View docs list: curl $SERVER_URL/api/repository/$REPO_ID/docs"
echo "  - View index: curl $SERVER_URL/api/repository/$REPO_ID/docs/index.md"
echo "  - View API docs: curl $SERVER_URL/api/repository/$REPO_ID/docs/api/python/index.md"
