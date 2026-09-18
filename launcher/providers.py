"""Language-model providers offered by the MASON setup wizard.

Provider ids are the ones opencode resolves through models.dev, so the agent framework needs no custom provider
entry: the API key is exported as the environment variable listed in `env` and opencode finds the endpoint itself.
`base` and `kind` are used only by the wizard's own test request (OpenAI-style chat completion or Anthropic-style
messages). `regions` lists providers that run separate endpoints in China and abroad; each region has its own
opencode provider id. The model lists were checked against models.dev on 2026-09-16; the wizard also accepts any
model id typed by hand.
"""

PROVIDERS = [
    {"id": "openai", "name": "OpenAI", "env": "OPENAI_API_KEY", "kind": "openai",
     "base": "https://api.openai.com/v1", "site": "https://platform.openai.com/api-keys",
     "models": ["gpt-5.6", "gpt-6-astra", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]},
    {"id": "anthropic", "name": "Anthropic (Claude)", "env": "ANTHROPIC_API_KEY", "kind": "anthropic",
     "base": "https://api.anthropic.com", "site": "https://console.anthropic.com/settings/keys",
     "models": ["claude-sonnet-5", "claude-opus-5", "claude-fable-5-1", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]},
    {"id": "google", "name": "Google (Gemini)", "env": ["GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"],
     "kind": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "site": "https://aistudio.google.com/apikey",
     "models": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro"]},
    {"id": "xai", "name": "xAI (Grok)", "env": "XAI_API_KEY", "kind": "openai",
     "base": "https://api.x.ai/v1", "site": "https://console.x.ai",
     "models": ["grok-4.6", "grok-4.5", "grok-4.3"]},
    {"id": "mistral", "name": "Mistral", "env": "MISTRAL_API_KEY", "kind": "openai",
     "base": "https://api.mistral.ai/v1", "site": "https://console.mistral.ai/api-keys",
     "models": ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"]},
    {"id": "deepseek", "name": "DeepSeek", "env": "DEEPSEEK_API_KEY", "kind": "openai",
     "base": "https://api.deepseek.com/v1", "site": "https://platform.deepseek.com/api_keys",
     "models": ["deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"]},
    {"id": "alibaba", "name": "Alibaba Cloud (Qwen)", "env": "DASHSCOPE_API_KEY", "kind": "openai",
     "site": "https://bailian.console.aliyun.com",
     "regions": [{"label": "China (dashscope.aliyuncs.com)", "id": "alibaba-cn", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
                 {"label": "International (dashscope-intl.aliyuncs.com)", "id": "alibaba", "base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"}],
     "models": ["qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "qwen3.7-max"]},
    {"id": "moonshotai", "name": "Moonshot AI (Kimi)", "env": "MOONSHOT_API_KEY", "kind": "openai",
     "site": "https://platform.moonshot.cn",
     "regions": [{"label": "China (api.moonshot.cn)", "id": "moonshotai-cn", "base": "https://api.moonshot.cn/v1"},
                 {"label": "International (api.moonshot.ai)", "id": "moonshotai", "base": "https://api.moonshot.ai/v1"}],
     "models": ["kimi-k3", "kimi-k2.6"]},
    {"id": "zhipuai", "name": "Zhipu AI (GLM)", "env": "ZHIPU_API_KEY", "kind": "openai",
     "site": "https://open.bigmodel.cn",
     "regions": [{"label": "China (open.bigmodel.cn)", "id": "zhipuai", "base": "https://open.bigmodel.cn/api/paas/v4"},
                 {"label": "International (api.z.ai)", "id": "zai", "base": "https://api.z.ai/api/paas/v4"}],
     "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2"]},
    {"id": "minimax", "name": "MiniMax", "env": "MINIMAX_API_KEY", "kind": "anthropic",
     "site": "https://platform.minimaxi.com",
     "regions": [{"label": "China (api.minimaxi.com)", "id": "minimax-cn", "base": "https://api.minimaxi.com/anthropic"},
                 {"label": "International (api.minimax.io)", "id": "minimax", "base": "https://api.minimax.io/anthropic"}],
     "models": ["MiniMax-M3", "MiniMax-M2.7"]},
    {"id": "volcengine", "name": "ByteDance Volcengine Ark (Doubao)", "env": "ARK_API_KEY", "kind": "openai",
     "base": "https://ark.cn-beijing.volces.com/api/v3", "site": "https://console.volcengine.com/ark",
     "models": ["doubao-seed-2-1-pro-260628", "doubao-seed-2-1-turbo-260628", "deepseek-v4-pro-ga-260813"]},
    {"id": "openrouter", "name": "OpenRouter (many models with one key)", "env": "OPENROUTER_API_KEY", "kind": "openai",
     "base": "https://openrouter.ai/api/v1", "site": "https://openrouter.ai/keys",
     "models": ["openai/gpt-5.6", "anthropic/claude-sonnet-5", "google/gemini-3.8-flash", "x-ai/grok-4.6",
                "deepseek/deepseek-v4-pro", "qwen/qwen3.8-flash", "moonshotai/kimi-k3", "z-ai/glm-5.3"]},
    {"id": "ollama", "name": "Ollama (models running on this computer)", "env": None, "kind": "openai",
     "base": "http://127.0.0.1:11434/v1", "site": "https://ollama.com",
     "models": ["qwen3.6:27b", "deepseek-v4-flash", "gpt-oss:20b"], "local": True},
]

LATER = "Configure later in the web interface"


def find(pid):
    """The provider entry (and region, if any) that owns opencode provider id `pid`."""
    for p in PROVIDERS:
        if p["id"] == pid and not p.get("regions"):
            return p, None
        for r in p.get("regions", []):
            if r["id"] == pid:
                return p, r
    return None, None


def env_names(p):
    e = p.get("env")
    return [] if not e else ([e] if isinstance(e, str) else list(e))
