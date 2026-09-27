"""Editable starter guidance, not a nutrient calculator or an allergy policy."""
from copy import deepcopy

NORWEGIAN_PRESET_NAME = 'Norwegian dietary guidelines (Helsedirektoratet, 2024; adult reference quantities)'
NORWEGIAN_PRESET = [
    NORWEGIAN_PRESET_NAME,
    'Choose varied, mostly plant-based meals with vegetables, wholegrains and legumes. Prefer fish and legumes more often than red meat. Minimize processed red AND white meat (for example bacon, salami, meat sausages and chicken nuggets); these are not routine automatic dinner choices. Plain mince without added salt/water is not processed meat. An explicitly requested occasional dish is possible within other restrictions.',
    'Adult whole-diet daily references: at least 500 g and preferably 800 g fruit, berries and vegetables; potatoes do not count toward this amount. At least 90 g actual wholegrain (not the weight of a wholegrain product), 20–30 g unsalted nuts, and three portions of lower-fat milk/dairy. Adapt to personal exclusions and suitable fortified alternatives where needed.',
    'Adult whole-diet weekly references: 300–450 g fish, including at least 200 g oily fish; no more than 350 g red meat. Fish/meat amounts refer to ready-to-eat edible quantities after preparation, not raw shopping weights; shellfish does not count toward the fish target. Include legumes as a main meal at least once a week, as well as sides or spreads.',
    'Prefer unsaturated plant oils or suitable soft margarine over butter, hard fats and tropical oils. Limit sweets, snacks and foods high in salt, sugar or saturated fat; drink water. Apply these choices to the requested meals, not as full-week quotas for a short dinner plan. Do not add unrelated groceries to fill unreported whole-diet gaps or claim full compliance from incomplete evidence.',
]
LEGACY_COUNTRY_PATTERN = 'National dietary guidelines for the household country'
SOURCES = [
    'https://www.helsenorge.no/kosthold-og-ernaring/kostradene/',
    'https://www.helsedirektoratet.no/faglige-rad/kostradene-og-naeringsstoffer/kostrad-for-befolkningen',
]


def dietary_guidance(profile):
    diet = profile.get('diet') or {}
    patterns = diet.get('patterns') or []
    if patterns == NORWEGIAN_PRESET:
        return {
            'status': 'norwegian_starter_preset', 'reviewed_on': '2026-09-27',
            'sources': deepcopy(SOURCES), 'whole_diet_compliance': 'not_established',
            'setup_explanation': 'The starting goal is the Norwegian dietary guidelines, with adult whole-diet reference amounts. Keep it, choose another country/pattern, edit it or remove it; no setup research is needed to use this preset.',
            'selection_review': 'Apply the saved text to actual ingredients and cooking methods before saving. For ordinary suggestions choose suitable fish/legume and vegetable-rich dishes over processed-meat dishes. An explicit occasional request or using reported stock may justify an exception; explain it without changing standing preferences. Respect personal restrictions and additional goals first. Service readiness/ranking does not verify guideline compliance.',
        }
    if LEGACY_COUNTRY_PATTERN in patterns:
        return {
            'status': 'country_needs_clarification', 'whole_diet_compliance': 'not_established',
            'setup_explanation': 'The saved national-guidelines goal does not identify a country. Confirm the intended country or offer the Norwegian starter preset once; preserve other saved preferences. Do not silently replace the existing goal or claim compliance.',
        }
    return {
        'status': 'custom_or_no_pattern', 'whole_diet_compliance': 'not_established',
        'selection_review': 'Use the actual saved dietary text, restrictions and goals. Do not reintroduce a replaced or cleared preset, invent numeric requirements, or claim service readiness verifies nutritional compliance.',
    }
