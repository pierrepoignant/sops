"""AI-generated training questions for a SOP, via the Claude API.

Config: ANTHROPIC__API_KEY (config section 'anthropic', see env_loader) or the
standard ANTHROPIC_API_KEY environment variable.
"""
import json
import os

from flask import current_app

from help.search import html_to_text

MODEL = 'claude-opus-4-8'

QUESTIONS_SCHEMA = {
    'type': 'object',
    'properties': {
        'questions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'question': {'type': 'string'},
                    'options': {
                        'type': 'array',
                        'items': {'type': 'string'},
                    },
                    'correct_index': {'type': 'integer'},
                    'explanation': {'type': 'string'},
                },
                'required': ['question', 'options', 'correct_index',
                             'explanation'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['questions'],
    'additionalProperties': False,
}


def is_configured():
    return bool(_api_key())


def _api_key():
    cfg = current_app.config.get('anthropic') or {}
    return cfg.get('api_key') or os.environ.get('ANTHROPIC_API_KEY')


def generate_questions(article, count=10, existing_questions=None):
    """Ask Claude for ``count`` multiple-choice questions about the article.
    Returns a list of dicts {question, options, correct_index, explanation}.
    Raises RuntimeError with a user-displayable message on failure."""
    import anthropic

    api_key = _api_key()
    if not api_key:
        raise RuntimeError("La clé API Anthropic n'est pas configurée "
                           "(ANTHROPIC__API_KEY).")

    body_text = html_to_text(article.body_html)[:30000]
    existing = [q.question for q in (existing_questions or [])]
    existing_block = ''
    if existing:
        listed = '\n'.join(f'- {q}' for q in existing[:60])
        existing_block = (
            "\n\nQuestions déjà proposées — n'en génère PAS de similaires :\n"
            f"{listed}")

    prompt = (
        "Tu prépares un quiz de formation interne pour les employés d'une "
        "boutique/atelier. À partir de la procédure (SOP) ci-dessous, génère "
        f"exactement {count} questions à choix multiples en français.\n\n"
        "Règles :\n"
        "- Chaque question teste un point opérationnel concret de la "
        "procédure (pas de trivia sur la formulation du texte).\n"
        "- 4 options par question, une seule correcte, les distracteurs "
        "doivent être plausibles.\n"
        "- Varie la position de la bonne réponse.\n"
        "- L'explication justifie la bonne réponse en une ou deux phrases, "
        "en citant la procédure.\n"
        f"{existing_block}\n\n"
        f"Titre de la procédure : {article.title}\n"
        f"Catégorie : {article.category}\n\n"
        f"Contenu de la procédure :\n{body_text}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={'type': 'adaptive'},
            output_config={'format': {'type': 'json_schema',
                                      'schema': QUESTIONS_SCHEMA}},
            messages=[{'role': 'user', 'content': prompt}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError('Clé API Anthropic invalide.')
    except anthropic.RateLimitError:
        raise RuntimeError('Limite de débit Anthropic atteinte — réessayez '
                           'dans une minute.')
    except anthropic.APIStatusError as e:
        raise RuntimeError(f'Erreur API Anthropic ({e.status_code}).')
    except anthropic.APIConnectionError:
        raise RuntimeError("Impossible de joindre l'API Anthropic.")

    if response.stop_reason == 'refusal':
        raise RuntimeError('La génération a été refusée par le modèle.')

    text = next((b.text for b in response.content if b.type == 'text'), '')
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError('Réponse du modèle illisible — réessayez.')

    questions = []
    for q in data.get('questions', []):
        options = [str(o) for o in q.get('options', [])]
        ci = q.get('correct_index', 0)
        if len(options) < 2 or not (0 <= ci < len(options)):
            continue
        questions.append({
            'question': str(q.get('question', '')).strip(),
            'options': options,
            'correct_index': ci,
            'explanation': str(q.get('explanation', '')).strip(),
        })
    if not questions:
        raise RuntimeError("Le modèle n'a produit aucune question valide.")
    return questions[:count]
