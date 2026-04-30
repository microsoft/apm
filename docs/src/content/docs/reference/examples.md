---
title: "Examples"
sidebar:
  order: 5
---

This guide showcases real-world APM workflows, from simple automation to enterprise-scale AI development patterns. Learn through practical examples that demonstrate the power of structured AI workflows.

> **Note:** Examples using `apm run` reference APM's experimental [Agent Workflows](../../guides/agent-workflows/) feature.

## Before & After: Traditional vs APM

### Traditional Approach (Unreliable)

**Manual Prompting**:
```
"Add authentication to the API"
```

**Problems**:
- Inconsistent results each time
- No context about existing code
- Manual guidance required for each step
- No reusable patterns
- Different developers get different implementations

### APM Approach (Reliable)

**Structured Workflow** (.prompt.md):
```yaml
---
description: Implement secure authentication system
mode: backend-dev
mcp:
  - ghcr.io/github/github-mcp-server
input: [auth_method, session_duration]
---

# Secure Authentication Implementation

## Context Loading
Review `security standards` and `existing auth patterns`.

## Implementation Requirements
- Use ${input:auth_method} authentication 
- Session duration: ${input:session_duration}
- Follow `security checklist`

## Validation Gates
🚨 **STOP**: Confirm security review before implementation

## Implementation Steps
1. Set up JWT token system with proper secret management
2. Implement secure password hashing using bcrypt
3. Create session management with Redis backend
4. Add logout and token revocation functionality
5. Implement rate limiting on auth endpoints
6. Add comprehensive logging for security events

## Testing Requirements
- Unit tests for all auth functions
- Integration tests for complete auth flow
- Security penetration testing
- Load testing for auth endpoints
```

**Execute**:
```bash
apm run implement-auth --param auth_method=jwt --param session_duration=24h
```

**Benefits**:
- Consistent, reliable results
- Contextual awareness of existing codebase
- Security standards automatically applied
- Reusable across projects
- Team knowledge embedded

## Multi-Step Feature Development

APM enables complex workflows that chain multiple AI interactions:

### Example: Complete Feature Implementation

```bash
# 1. Generate specification from requirements
apm run create-spec --param feature="user-auth"
```

```yaml
# .apm/prompts/create-spec.prompt.md
---
description: Generate technical specification from feature requirements
mode: architect
input: [feature]
---

# Technical Specification Generator

## Requirements Analysis
Generate a comprehensive technical specification for: ${input:feature}

## Specification Sections Required
1. **Functional Requirements** - What the feature must do
2. **Technical Design** - Architecture and implementation approach  
3. **API Contracts** - Endpoints, request/response formats
4. **Database Schema** - Data models and relationships
5. **Security Considerations** - Authentication, authorization, validation
6. **Testing Strategy** - Unit, integration, and e2e test plans
7. **Performance Requirements** - Load expectations and optimization
8. **Deployment Plan** - Rollout strategy and monitoring

## Context Sources
- Review `existing architecture`
- Follow `API design standards`
- Apply `security guidelines`

## Output Format
Create `specs/${input:feature}.spec.md` following our specification template.
```

```bash
# 2. Review and validate specification  
apm run review-spec --param spec="specs/user-auth.spec.md"
```

```bash
# 3. Implement feature following specification
apm run implement --param spec="specs/user-auth.spec.md"
```

```bash
# 4. Generate comprehensive tests
apm run test-feature --param feature="user-authentication"
```

Each step leverages your project's Context for consistent, reliable results that build upon each other.

## Enterprise Use Cases

### Legal Compliance Package

**Scenario**: Fintech company needs GDPR compliance across all projects

```yaml
# .apm/instructions/gdpr-compliance.instructions.md
---
applyTo: "**/*.{py,js,ts}"
---

# GDPR Compliance Standards

## Data Processing Requirements
- Explicit consent for all data collection
- Data minimization principles
- Right to be forgotten implementation
- Data portability support
- Breach notification within 72 hours

## Implementation Checklist
- [ ] Personal data encryption at rest and in transit
- [ ] Audit logging for all data access
- [ ] User consent management system
- [ ] Data retention policies enforced
- [ ] Regular security assessments scheduled

## Code Pattern Requirements
```python
# Required pattern for user data handling
@gdpr_compliant
@audit_logged
def process_user_data(user_data: UserData, consent: ConsentRecord):
    validate_consent(consent)
    return secure_process(user_data)
```
```

```yaml  
# .apm/prompts/gdpr-audit.prompt.md
---
description: Comprehensive GDPR compliance audit
mode: legal-compliance
input: [scope]
---

# GDPR Compliance Audit

## Audit Scope
Review ${input:scope} for GDPR compliance violations.

## Audit Areas
1. **Data Collection Points** - Identify all user data capture
2. **Consent Management** - Verify explicit consent mechanisms  
3. **Data Storage** - Check encryption and access controls
4. **Data Processing** - Validate lawful basis for processing
5. **User Rights** - Confirm right to access/delete/portability
6. **Breach Response** - Verify notification procedures

## Compliance Report
Generate detailed findings with:
- ✅ Compliant areas
- ⚠️ Areas needing attention  
- ❌ Critical violations requiring immediate action
- 📋 Recommended remediation steps
```

**Usage across projects**:
```bash
# Audit new feature for compliance
apm run gdpr-audit --param scope="user-profile-feature"

# Generate compliance documentation  
apm run compliance-docs --param regulations="GDPR,CCPA"
```

### Code Review Package

**Scenario**: Engineering team needs consistent code quality standards

```yaml
# .apm/chatmodes/senior-reviewer.chatmode.md
---
name: "Senior Code Reviewer"
model: "gpt-4"
tools: ["file-manager", "git-analysis"]
expertise: ["security", "performance", "maintainability"]
---

You are a senior software engineer with 10+ years experience conducting thorough code reviews. 

## Review Focus Areas
- **Security**: Identify vulnerabilities and attack vectors
- **Performance**: Spot efficiency issues and optimization opportunities  
- **Maintainability**: Assess code clarity, documentation, and structure
- **Best Practices**: Enforce team coding standards and patterns

## Review Style
- Constructive and educational feedback
- Specific, actionable recommendations
- Code examples for suggested improvements
- Balance between thoroughness and development velocity
```

```yaml
# .apm/prompts/security-review.prompt.md
---  
description: Comprehensive security code review
mode: senior-reviewer
input: [files, severity_threshold]
---

# Security Code Review

## Review Scope  
Analyze ${input:files} for security vulnerabilities with ${input:severity_threshold} minimum severity.

## Security Checklist
- [ ] **Input Validation** - All user inputs properly sanitized
- [ ] **Authentication** - Secure authentication implementation
- [ ] **Authorization** - Proper access control enforcement
- [ ] **Encryption** - Sensitive data encrypted appropriately
- [ ] **SQL Injection** - Parameterized queries used
- [ ] **XSS Prevention** - Output properly encoded
- [ ] **CSRF Protection** - Anti-CSRF tokens implemented
- [ ] **Secrets Management** - No hardcoded credentials

## Report Format
For each finding provide:
1. **Severity Level** (Critical/High/Medium/Low)
2. **Vulnerability Description** - What the issue is
3. **Impact Assessment** - Potential consequences
4. **Code Location** - Exact file and line numbers
5. **Remediation Steps** - How to fix the issue
6. **Example Fix** - Code showing the correction
```

**Team Usage**:
```bash
# Pre-merge security review
apm run security-review --param files="src/auth/**" --param severity_threshold="medium"

# Performance review for critical path
apm run performance-review --param files="src/payment-processing/**"

# Full feature review before release  
apm run feature-review --param feature="user-dashboard"
```

### Onboarding Package

**Scenario**: Quickly get new developers productive with company standards

```yaml
# .apm/instructions/company-standards.instructions.md
---
description: Development standards for all AcmeCorp projects
applyTo: "**/*"
---

# Development Standards at AcmeCorp

## Tech Stack
- **Backend**: Python FastAPI, PostgreSQL, Redis
- **Frontend**: React TypeScript, Tailwind CSS  
- **Infrastructure**: AWS, Docker, Kubernetes
- **CI/CD**: GitHub Actions, Terraform

## Code Organization
- Domain-driven design with clean architecture
- Repository pattern for data access
- Event-driven communication between services
- Comprehensive testing with pytest and Jest

## Security Standards  
- Zero-trust security model
- All API endpoints require authentication
- Sensitive data encrypted with AES-256
- Regular security audits and penetration testing
```

```yaml
# .apm/prompts/onboard-developer.prompt.md
---
description: Interactive developer onboarding experience
mode: tech-lead  
input: [developer_name, role, experience_level]
---

# Welcome ${input:developer_name}! 

## Your Onboarding Journey
Welcome to the engineering team! I'll help you get productive quickly.

**Your Role**: ${input:role}
**Experience Level**: ${input:experience_level}

## Step 1: Environment Setup
Let me guide you through setting up your development environment:

1. **Repository Access** - Clone main repositories
2. **Local Development** - Set up Docker development environment  
3. **IDE Configuration** - Configure VSCode with team extensions
4. **Database Setup** - Connect to development database
5. **API Keys** - Set up necessary service credentials

## Step 2: Codebase Tour
I'll walk you through our architecture:
- `Company Standards`
- `API Patterns` 
- `Testing Guidelines`

## Step 3: First Tasks
Based on your experience level, here are your starter tasks:
${experience_level == "senior" ? "Architecture review and team mentoring" : "Bug fixes and small feature implementation"}

## Step 4: Team Integration
- Schedule 1:1s with team members
- Join relevant Slack channels
- Set up recurring team meetings

Ready to start? Let's begin with environment setup!
```

**Usage**:
```bash
# Personalized onboarding for new hire
apm run onboard-developer \
  --param developer_name="Alice" \
  --param role="Backend Engineer" \
  --param experience_level="mid-level"
```

## Real-World Workflow Patterns

### API Development Workflow

Complete API development from design to deployment:

```bash
# 1. Design API specification
apm run api-design --param endpoint="/users" --param operations="CRUD"

# 2. Generate implementation skeleton  
apm run api-implement --param spec="specs/users-api.spec.md"

# 3. Add comprehensive tests
apm run api-tests --param endpoint="/users"

# 4. Security review
apm run security-review --param files="src/api/users/**"

# 5. Performance optimization
apm run optimize-performance --param endpoint="/users" --param target_latency="100ms"

# 6. Documentation generation
apm run api-docs --param spec="specs/users-api.spec.md"
```

### Bug Fix Workflow

Systematic approach to bug resolution:

```bash
# 1. Bug analysis and reproduction
apm run analyze-bug --param issue_id="GH-123"

# 2. Root cause investigation  
apm run root-cause --param symptoms="slow_api_response" --param affected_endpoints="/search"

# 3. Fix implementation with tests
apm run implement-fix --param bug_analysis="analysis/GH-123.md"

# 4. Regression testing
apm run regression-test --param fix_areas="search,performance"

# 5. Release preparation
apm run prepare-hotfix --param fix_id="GH-123" --param target_environment="production"
```

### Documentation Workflow

Keep documentation synchronized with code:

```bash
# Auto-update docs when code changes
apm run sync-docs --param changed_files="src/api/**"

# Generate comprehensive API documentation
apm run generate-api-docs --param openapi_spec="openapi.yaml"

# Create tutorial from working examples  
apm run create-tutorial --param example_dir="examples/authentication"

# Update architecture diagrams
apm run update-architecture --param components="auth,payment,user-management"
```

## Performance Optimization Examples

### High-Performance Code Generation

```yaml
# .apm/prompts/optimize-performance.prompt.md
---
description: Optimize code for performance and scalability
mode: performance-engineer
input: [target_files, performance_goals]
---

# Performance Optimization

## Optimization Targets
Files: ${input:target_files}
Goals: ${input:performance_goals}

## Analysis Areas
1. **Algorithm Complexity** - Identify O(n²) operations
2. **Database Queries** - Find N+1 query problems
3. **Memory Usage** - Spot memory leaks and inefficient allocations
4. **I/O Operations** - Optimize file and network operations
5. **Caching Opportunities** - Add strategic caching layers

## Optimization Techniques
- Database query optimization with proper indexing
- Implement response caching with Redis  
- Add database connection pooling
- Optimize serialization/deserialization
- Implement lazy loading for expensive operations
- Add performance monitoring and alerting

## Benchmarking
Before and after performance measurements required:
- Response time percentiles (p50, p95, p99)
- Memory usage patterns
- CPU utilization under load
- Database query execution times
```

## Advanced Enterprise Patterns  

### Multi-Repository Consistency

**Scenario**: Ensure consistency across microservices

```bash
# Synchronize API contracts across services
apm run sync-contracts --param services="user-service,payment-service,notification-service"

# Update shared libraries across repositories
apm run update-shared-libs --param version="2.1.0" --param repositories="all-backend-services"

# Consistent logging and monitoring setup
apm run setup-observability --param services="production-services" --param monitoring_level="full"
```

### Compliance and Governance

```bash
# Regular compliance audits
apm run compliance-audit --param regulations="SOX,GDPR,PCI-DSS" --param scope="financial-services"

# Security posture assessment
apm run security-assessment --param severity="all" --param scope="customer-facing-apis"  

# Code quality governance
apm run quality-gate --param threshold="A" --param coverage_min="85%" --param security_scan="required"
```

## Next Steps

Ready to build your own workflows? Check out:

- **[Context Guide](../../introduction/key-concepts/)** - Learn to build custom workflows
- **[Integrations Guide](../../integrations/ide-tool-integration/)** - Connect with your existing tools
- **[Getting Started](../../getting-started/installation/)** - Set up your first project

Or explore the complete framework at [AI-Native Development Guide](https://danielmeppiel.github.io/awesome-ai-native/)!