#!/bin/bash

# Generate a random filename using random number
generate_name() {
    echo "/tmp/claude_statusline_${RANDOM}_${RANDOM}.json"
}

TMPJSON=$(generate_name)

# Loop until we find a filename that does NOT exist
while [[ -e "$TMPJSON" ]]; do
    TMPJSON=$(generate_name)
done

# Capture stdin (piped JSON) into the temp file
cat > "$TMPJSON"

# Extract fields using jq
MODEL=$(jq -r '.model.display_name' "$TMPJSON")

PCT=$(jq -r '.context_window.used_percentage // 0 | floor' "$TMPJSON")
USED=$(jq -r '.context_window.total_input_tokens // 0 | floor' "$TMPJSON")
MAX=$(jq -r '.context_window.context_window_size // 0 | floor' "$TMPJSON")
CACHE_READ=$(jq -r '.context_window.current_usage.cache_read_input_tokens // 0 | floor' "$TMPJSON")
CACHE_WRITE=$(jq -r '.context_window.current_usage.cache_creation_input_tokens // 0 | floor' "$TMPJSON")
CURDIR=$(jq -r '.workspace.current_dir' "$TMPJSON")

# Get only the folder name
DIRNAME=$(basename "$CURDIR")

# Get git branch
BRANCH=$(git branch --show-current 2>/dev/null)

# Format used tokens with a lowercase k (rounded to nearest 1k): 14000 -> 14k
fmt_used() {
    local n=$1
    if (( n >= 1000 )); then
        echo "$(( (n + 500) / 1000 ))k"
    else
        echo "$n"
    fi
}

# Format with an uppercase K/M (rounded): 1000000 -> 1M, 200000 -> 200K, 10800 -> 11K
fmt_big() {
    local n=$1
    if (( n >= 1000000 )); then
        if (( n % 1000000 == 0 )); then
            echo "$(( n / 1000000 ))M"
        else
            awk "BEGIN{printf \"%.1fM\", $n/1000000}"
        fi
    elif (( n >= 1000 )); then
        echo "$(( (n + 500) / 1000 ))K"
    else
        echo "$n"
    fi
}

# Build the context segment: "6% 60k/1M - r:10K w:40K" (fall back to just the % if size unknown)
# r: tokens read from the prompt cache (cache_read_input_tokens)
# w: tokens written to the prompt cache this turn (cache_creation_input_tokens)
if (( MAX > 0 )); then
    CTX="$PCT% $(fmt_used "$USED")/$(fmt_big "$MAX") - r:$(fmt_big "$CACHE_READ") w:$(fmt_big "$CACHE_WRITE")"
else
    CTX="$PCT%"
fi

# Output formatting
if [ -n "$BRANCH" ]; then
    echo "🤖 $MODEL | 📊 $CTX | 📁 $DIRNAME | 🌿 $BRANCH"
else
    echo "🤖 $MODEL | 📊 $CTX | 📁 $DIRNAME"
fi

# Cleanup
rm "$TMPJSON"