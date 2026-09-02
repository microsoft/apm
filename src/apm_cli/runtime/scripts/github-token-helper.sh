#!/bin/bash
#
# GitHub Token Helper - Standalone shell implementation
# 
# TOKEN PRECEDENCE RULES (AUTHORITATIVE):
# ======================================
# 1. GitHub Models: GITHUB_TOKEN > GITHUB_APM_PAT   
# 2. APM Modules: GITHUB_APM_PAT > GITHUB_TOKEN 

# CRITICAL: Never overwrite existing GITHUB_TOKEN (Models access)
#

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Setup GitHub tokens with proper precedence and preservation
setup_github_tokens() {
    local quiet_mode="${1:-false}"
    
    if [[ "$quiet_mode" != "true" ]]; then
        echo -e "${BLUE}Setting up GitHub tokens...${NC}"
    fi
    
    # CRITICAL: Preserve existing GITHUB_TOKEN if set (for Models access)
    local preserve_github_token=""
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        preserve_github_token="$GITHUB_TOKEN"
        if [[ "$quiet_mode" != "true" ]]; then
            echo -e "${GREEN}✓ Preserving existing GITHUB_TOKEN for Models access (${#GITHUB_TOKEN} chars)${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️  No GITHUB_TOKEN found initially${NC}"
    fi
    
    # 2. Setup APM module access
    # Precedence: GITHUB_APM_PAT > GITHUB_TOKEN
    if [[ -z "${GITHUB_APM_PAT:-}" ]]; then
        if [[ -n "${GITHUB_TOKEN:-}" ]]; then
            export GITHUB_APM_PAT="${GITHUB_TOKEN}"
        fi
    fi
    
    # 3. Setup Models access (GITHUB_TOKEN for Codex, GITHUB_MODELS_KEY for LLM)
    # Precedence: GITHUB_TOKEN > GITHUB_APM_PAT
    # CRITICAL: Only set GITHUB_TOKEN if not already present (never overwrite)
    if [[ -z "${GITHUB_TOKEN:-}" ]]; then
        if [[ -n "${GITHUB_APM_PAT:-}" ]]; then
            export GITHUB_TOKEN="${GITHUB_APM_PAT}"
        fi
    fi
    
    # 4. Restore preserved GITHUB_TOKEN (never overwrite Models-enabled token)
    if [[ -n "$preserve_github_token" ]]; then
        export GITHUB_TOKEN="$preserve_github_token"
    fi
    
    # 5. Setup LLM Models key
    if [[ -n "${GITHUB_TOKEN:-}" ]] && [[ -z "${GITHUB_MODELS_KEY:-}" ]]; then
        export GITHUB_MODELS_KEY="${GITHUB_TOKEN}"
    fi

    if [[ "$quiet_mode" != "true" ]]; then
        echo -e "${GREEN}✅ GitHub token environment configured${NC}"
    fi
}

# Get appropriate token for specific runtime
get_token_for_runtime() {
    local runtime="$1"
    
    case "$runtime" in
        "codex"|"models"|"llm")
            # Models: GITHUB_TOKEN > GITHUB_APM_PAT
            if [[ -n "${GITHUB_TOKEN:-}" ]]; then
                echo "$GITHUB_TOKEN"
            elif [[ -n "${GITHUB_APM_PAT:-}" ]]; then
                echo "$GITHUB_APM_PAT"
            fi
            ;;
        *)
            # General: GITHUB_APM_PAT > GITHUB_TOKEN
            if [[ -n "${GITHUB_APM_PAT:-}" ]]; then
                echo "$GITHUB_APM_PAT"
            elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
                echo "$GITHUB_TOKEN"
            fi
            ;;
    esac
}

# Validate GitHub tokens
validate_github_tokens() {
    local has_any_token=false
    local has_models_token=false
    
    if [[ -n "${GITHUB_APM_PAT:-}" ]] || [[ -n "${GITHUB_TOKEN:-}" ]]; then
        has_any_token=true
    fi
    
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        has_models_token=true
    fi
    
    if [[ "$has_any_token" == "false" ]]; then
        echo -e "${RED}❌ No GitHub tokens found${NC}"
        echo "Required: Set one of these environment variables:"
        echo "  GITHUB_TOKEN (user-scoped PAT for GitHub Models)"
        echo "  GITHUB_APM_PAT (fine-grained PAT for APM modules)"  
        return 1
    fi
    
    if [[ "$has_models_token" == "false" ]]; then
        echo -e "${YELLOW}⚠️  Warning: No user-scoped PAT found. GitHub Models API may not work with fine-grained PATs.${NC}"
        echo "For full functionality, set GITHUB_TOKEN to a user-scoped PAT."
        return 1
    fi
    
    echo -e "${GREEN}✅ GitHub token validation passed${NC}"
    return 0
}

# Export functions for use in other scripts
export -f setup_github_tokens
export -f get_token_for_runtime
export -f validate_github_tokens
