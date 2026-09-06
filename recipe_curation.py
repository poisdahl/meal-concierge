"""Offline editorial completion; source text stays immutable and estimates explicit.

This is a publisher build step, never an ordinary import interpretation. Numeric
recovery precedes culinary assumptions. Unhandled food/procedure cases remain
visible to the editor and cannot acquire project review by default.
"""
from copy import deepcopy
from fractions import Fraction
import re
import unicodedata

from recipe_quantities import UNITS, quantity_json, read_quantity

class SourceMethodExcluded(Exception):
    """A source-bound editorial decision excludes a recipe with no real method."""


POLICY = 'meal-concierge-culinary-completion-v1'
NUMBER = r'(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
PREFIX = re.compile(r'^(' + NUMBER + r')(?:\s*[–—-]\s*(' + NUMBER + r'))?\s*(.*)$')
ALIASES = {
    'tbs': 'tbsp', 'tbls': 'tbsp', 'tblsp': 'tbsp', 'tb': 'tbsp',
    'tablespoons': 'tbsp', 'tablespoon': 'tbsp', 'teaspoons': 'tsp', 'teaspoon': 'tsp',
    'milligram': 'mg', 'milligrams': 'mg', 'tbl': 'tbsp', 'kilo': 'kg', 'kilos': 'kg', 'qt': 'quart', 'qts': 'quarts', 'pt': 'pint', 'pts': 'pints', 'fluid oz': 'fl oz', 'tsps': 'tsp', 'tbsps': 'tbsp', 'ounces': 'oz', 'ounce': 'oz', 'pounds': 'lb', 'pound': 'lb', 'lbs': 'lb', 'deciliters': 'dl', 'deciliter': 'dl',
}
GROUPS = [
 ('spice', r'\b(salt|peppercorn|black pepper|white pepper|ground pepper|cayenne|chili powder|chile powder|chilli powder|paprika|cumin|turmeric|cinnamon|nutmeg|mace|allspice|saffron|asafoetida|fenugreek|cardamom|cloves|baking soda|baking powder|yeast|cream of tartar|seasoning|spice|masala|kanwa|potash|yaji)\b'),
 ('herb', r'\b(parsley|cilantro|coriander|basil|thyme|oregano|rosemary|dill|sage|chives|mint|bay leaf|bay leaves|curry leaf|curry leaves|scent leaf|scent leaves|marjoram|savory|herbs|tarragon|screwpine|pandan|lemongrass)\b'),
 ('aromatic', r'\b(garlic|ginger|shallot|shallots|chilli|chillies|chile|chiles|chili|chilies|scotch bonnet|habanero|hot pepper|hot peppers)\b'),
 ('sugar', r'\b(sugar|honey|syrup|molasses|treacle|jaggery|sweetener|icing|jam|jelly|preserve|preserves|marmalade)\b'),
 ('fat', r'\b(oil|butter|margarine|ghee|lard|shortening|cooking spray|suet|dripping)\b'),
 ('liquid', r'\b(water|stock|broth|wine|beer|ale|cider|milk|juice|soda|seltzer|coffee|tea|rum|vodka|gin|brandy|whiskey|whisky|liqueur|bourbon|vermouth|tequila|champagne|cognac|absinthe|kirsch|sherry|port)\b'),
 ('sauce', r'\b(sauce|vinegar|mustard|ketchup|mayonnaise|mayo|tahini|chutney|pesto|paste|puree|purée|extract|vanilla|kecap|miso|tamarind|bouillon)\b'),
 ('egg', r'\b(egg|eggs|yolk|yolks|whites)\b'),
 ('dairy', r'\b(cheese|cheddar|parmesan|mozzarella|feta|ricotta|cream|yogurt|yoghurt|yoghourt|curd|quark|kefir|custard)\b'),
 ('protein', r'\b(beef|veal|pork|lamb|mutton|goat|meat|chicken|turkey|duck|goose|fish|salmon|tuna|cod|haddock|prawn|prawns|shrimp|shrimps|crab|lobster|clam|clams|mussel|mussels|oyster|oysters|squid|octopus|scallop|scallops|sardine|sardines|anchovy|anchovies|mackerel|tilapia|herring|trout|halibut|hake|eel|offal|liver|kidney|tripe|sausage|sausages|frankfurters|bacon|ham|chorizo|salami|pepperoni|tofu|tempeh|seitan|paneer|periwinkle|periwinkles|kpomo|stockfish|crayfish)\b'),
 ('carb', r'\b(rice|flour|semolina|cornmeal|corn meal|starch|cornstarch|cornflour|tapioca|sago|oat|oats|oatmeal|grain|grains|wheat|spelt|rye|barley|millet|sorghum|quinoa|couscous|bulgur|buckwheat|polenta|masa|rava|garri|fufu|noodles|noodle|pasta|macaroni|spaghetti|vermicelli|linguine|fettuccine|penne|lasagne|lasagna|lentils|lentil|dal|dhal|beans|bean|chickpeas|chickpea|maize|ukwa|breadfruit|achicha)\b'),
 ('bread', r'\b(bread|breadcrumbs|bread crumbs|toast|bun|buns|rolls|biscuit|biscuits|cracker|crackers|cookie|cookies|tortilla|tortillas|pita|pitta|muffin|muffins|bagel|bagels|crepe|crepes|crêpe|crêpes|pastry|dough|crust|wrappers|wonton|pretzels|wafer|wafers|croissant|croissants|puff pastry)\b'),
 ('nuts', r'\b(nut|nuts|walnut|walnuts|almond|almonds|pecan|pecans|hazelnut|hazelnuts|pistachio|pistachios|cashew|cashews|peanut|peanuts|groundnut|groundnuts|sesame|sunflower seeds|pumpkin seeds|poppy|linseed|flax|egusi|melon seeds|coconut)\b'),
 ('fruit', r'\b(apple|apples|pear|pears|banana|bananas|orange|oranges|tangerine|tangerines|lemon|lemons|lime|limes|grapefruit|mandarin|mandarins|berries|berry|strawberry|strawberries|raspberries|raspberry|blueberries|blackberries|cranberries|cherries|cherry|peach|peaches|plum|plums|apricot|apricots|grape|grapes|melon|watermelon|pineapple|mango|mangoes|papaya|pawpaw|passion fruit|kiwi|fig|figs|date|dates|raisin|raisins|sultana|sultanas|currant|currants|prune|prunes|durian|fruit|rhubarb|avocado|avocados)\b'),
 ('vegetable', r'\b(potato|potatoes|yam|yams|cassava|yuca|cocoyam|taro|plantain|plantains|carrot|carrots|tomato|tomatoes|onion|onions|leek|leeks|celery|celeriac|pepper|peppers|capsicum|cabbage|cauliflower|broccoli|spinach|greens|lettuce|kale|chard|okra|aubergine|eggplant|zucchini|courgette|cucumber|cucumbers|squash|pumpkin|beet|beets|beetroot|turnip|turnips|parsnip|parsnips|radish|radishes|mushroom|mushrooms|peas|pea|corn|sweetcorn|sweet corn|sprouts|asparagus|artichoke|artichokes|olive|olives|vegetable|vegetables|bamboo|seaweed|watercress|fennel|ewedu|bitterleaf|bitter leaf|oha|afang|yanrin|onugbu|ugba|ukpaka)\b'),
 ('chocolate', r'\b(chocolate|cocoa|cacao|carob)\b'),
]
GROUPS = [(key, re.compile(pattern, re.I)) for key, pattern in GROUPS]


def clean(text):
    text = re.sub(r'(?<=\d)([¼½¾⅓⅔⅛⅜⅝⅞])', r' \1', text or '')
    text = unicodedata.normalize('NFKC', text).replace('⁄', '/')
    text = re.sub(r'(?<=\d),(?=\d{3}\b)', '', text)
    text = re.sub(r'(?<=\d),(?=\d)', '.', text)
    text = re.sub(r'(?<=\d)\s+and\s+(?=\d+/)', ' ', text)
    text = re.sub(r'(?<=\d)\s+to\s+(?=\d)', '–', text)
    text = re.sub(r'\bhandfulls?\b', 'handful', text, flags=re.I)
    text = re.sub(r'\bt sp\b', 'tsp', text, flags=re.I)
    for word, number in [('a dozen and a half','18'),('half a dozen','6'),('a dozen','12'),('one dozen','12'),('a couple of','2'),('one','1'),('two','2'),('three','3'),('four','4'),('half','1/2'),('a quarter','1/4')]:
        text = re.sub(r'^' + word + r'\b', number, text, flags=re.I)
    return ' '.join(text.split()).strip(' .')


def fraction(text):
    if ' ' in text:
        whole, part = text.split()
        return Fraction(whole) + Fraction(part)
    return Fraction(text)


EXTRA_GROUPS = {
 'spice': r'caffeine|daddawa|dawa dawa|ogili|musk|ambergris|hops|gelatin|gelatine|wasabi|permento|peppercorns|spices|seasonings|curry powder|baharat|sumak|sumac|anise|caraway|nigella|juniper|panch puran|saz[oó]n|maggi|ogiri|uyayak|msg|monosodium|pectin|xanthan|lecithin|poultry shake|rib rub|steak rub|barbecue rub|chop rub|flavoring|flavouring|food coloring|food colorings|pigment|liquid smoke|edible.*(?:gold|silver)',
 'herb': r'epazote|lavender|lovage|bouquet garni|peppermint|utazi|uziza|eru leaf|soy leaves|pennyroyal',
 'aromatic': r'ají dulce|challots|jalape[ñn]o|galangal|galingal|ginseng|chillis|horseradish',
 'protein': r'hot dogs?|filet mignons?|hamburger patties|calf.s foot|pigs feet|game hens?|iguana|giblets|meatloaf|calamari|gizzard|frogs?|giblets|duck|lobsters|crabs|shrimps|haddock|fishballs|forcemeat|trotters|catfish|monkfish|barramundi|pigs trotters|hamburger patty|morcilla|black pudding|picadillo|soybean cake|meatballs|whitefish|seafood|pancetta|mortadella|poultry|fowl|flathead|conch|codfish|steak|oxtail|marrowbone|rabbit|hare|venison|eulachon|snails|shaki|ponmo|cow feet|dryfish|bonito|katsuobushi|sea urchin|quorn|spam',
 'vegetable': r'radicchio|kohlrabi|tomatillo|pimentos|capsicum|swede|pak koi|pickles?|gherkins|capers|sauerkraut|salad|rocket|pak choi|bok choy|scallions?|waterleaf|cocoyams|zucchinis|tomatos|hominy|sprout mix|petits pois|cress|konbu|kombu|kelp|nori',
 'sauce': r'hotsauce|salsa|sarza|vinaigrette|dressing|a[ïi]oli|catsup|gochujang|doenjang|nước mắm|sambal|sriracha|tabasco|tzatziki|guacamole|gravy|vegemite',
 'dairy': r'cheeselets?|imitation whipped topping|cool whip|mascarpone|parmigiano|pecorino|romano|monterey jack|cheeses|cr[eè]me fra[iî]che|chantilly',
 'sugar': r'candy|candies|kool.aid|piloncilo|twinkies|mars bar|frangipane|anko filling|dulce de leche|toffee|marshmallow|marzipan|nougat|buttercream|frosting|glaze|fudge|sprinkles|hundreds.and.thousands|meringue',
 'fruit': r'blackcurrants?|redcurrants?|nectarine|barberries|calamansi|hatxora|pomegranate|mixed peel|citrus peel|clementine|quinces|rosehip|jackfruit',
 'carb': r'(?:cake|brownie|baking) mix|popped popcorn|grits|locust beans|okpa|masarepa|noodles|risotto|tater.tots|hash browns|soybeans|pops cereal|matzo|sourdough starter|fermentation starter|fries|chips|homi',
 'bread': r'chapati|quiche|crusts|crust|phyllo|filo|shells?|brownie squares|kuli kuli|ladyfingers|rice paper|pie crust|taco shells|ciabatta|casabe|falafel|croutons|croûtons|french roll|rougag|injera|puris|bulla cakes|cannoli shells|saltines|breading|sponge cake|cake layers|yorkshire pudding',
 'nuts': r'candlenuts?|candlenut|4.spice powder|chestnuts|cottonseeds',
 'liquid': r'buttermilk|espresso|cafe la llave|rosewater|grenadine|ice|clear soup|soup mix|sake|liquor|dashi|cordial|cola|guinness|stout|rooibos|yerba mate|beverage',
 'fat': r'fat|pan drippings|fond from',
}


def group(item):
    # Ingredient head has priority over seasoning/preparation after a comma.
    head = re.split(r'\s+or\s+',item,flags=re.I)[0].split(',')[0]
    head=re.sub(r'^\([^)]*\)\s*','',head)
    if re.search(r'\bvinegar\b',head,re.I):return 'sauce'
    if re.search(r'cinnamon[- ]sugar',head,re.I):return 'sugar'
    if re.search(r'water chestnut',head,re.I):return 'vegetable'
    if re.search(r'^(?:drizzle of |spray |vegetable spray|cooking spray)',head,re.I):return 'fat'
    if re.search(r'chipotles?',head,re.I):return 'aromatic'
    if re.search(r'sugar snap|snow peas|green beans',head,re.I):return 'vegetable'
    if re.search(r'\bgarlic\b|ground ginger',head,re.I) and not re.search(r'powder|salt|oil|butter|sauce|bread',head,re.I):return 'aromatic'
    if re.search(r'butter beans|kidney beans',head,re.I):return 'carb'
    if re.search(r'\bsalt(?:ed)? (?:cod|fish|pork|beef)\b',head,re.I):return 'protein'
    if re.search(r'peanut butter|almond butter',head,re.I):return 'nuts'
    if re.search(r'water\s*(?:leaf|yam)|water fufu',head,re.I):return 'vegetable' if 'fufu' not in head.lower() else 'carb'
    if re.search(r'(?:stock|bouillon|seasoning).*cube|cube.*(?:stock|bouillon)',head,re.I):return 'spice'
    if re.search(r'dill pickles?',head,re.I):return 'vegetable'
    if re.fullmatch(r'(?:freshly[- ]ground )?pepper(?:\s+(?:to taste|as needed))?',head.strip(),re.I):return 'spice'
    for text in (head, re.sub(r'\b([A-Za-z]{3,})s\b',r'\1',head), item):
        result = next((name for name, rx in GROUPS if rx.search(text)), None)
        if result:return result
        result = next((name for name, pattern in EXTRA_GROUPS.items() if re.search(pattern,text,re.I)),None)
        if result:return result
    return None


def estimate(input_text, assumptions):
    return {'basis': 'estimate', 'input': input_text, 'assumptions': assumptions}


def source(input_text, conversion=None):
    return {'basis': 'source', 'input': input_text, 'conversion': conversion}


def cup_weight(item):
    """Editorial approximations in grams per 240 ml cup; never source facts."""
    t = item.casefold()
    if re.search(r'\boil\b|milk|juice|water|broth|stock|wine|cream(?! cheese)', t):return None
    for pattern, weight in [
        (r'flour|cornstarch|cornflour',120),(r'powdered sugar|icing sugar|confection',120),
        (r'sugar',200),(r'honey',340),(r'syrup|molasses|treacle',320),
        (r'butter|margarine|lard|shortening',225),(r'cooked.*rice',160),
        (r'dry.*rice|uncooked.*rice|\brice\b',185),(r'oats|oatmeal',90),(r'cooked.*beans|canned.*beans|chickpeas',170),
        (r'lentils|beans|dal|dhal',190),(r'grated.*cheese|shredded.*cheese|parmesan',100),
        (r'breadcrumb|bread crumbs',110),(r'cocoa',85),(r'chopped.*nuts|almond|walnut|pecan|peanut',140),
        (r'raisins|sultanas|currants',150),(r'coconut',85),(r'spinach|lettuce|parsley|cilantro|herbs',30),
    ]:
        if re.search(pattern,t):return weight
    return {'fat':220,'sugar':200,'herb':30,'aromatic':140,'dairy':240,'protein':150,'carb':170,'bread':100,'nuts':140,'fruit':160,'vegetable':150,'chocolate':170}.get(group(item))


def recovered(ingredient):
    """Recover source amounts and mark every interpretation/convention separately."""
    i = deepcopy(ingredient)
    original = i.get('original_text') or i['raw']
    if original.startswith('ingredient:'):
        heading=re.match(r'ingredient:\s*([^;]+)',original)
        if heading:i['item']=re.sub(r'\[\s*\d+\s*\]', '', heading[1]).strip()[:300]
    # Sum two explicit amounts of the same ingredient before accepting the
    # initial parser's first amount. Never split a different named ingredient.
    unit_words=r'kg|grams?|g|ml|litres?|l|oz|ounces?|lb|pounds?|cups?|tablespoons?|tbsp|teaspoons?|tsp'
    added=re.match(r'^('+NUMBER+r')\s*('+unit_words+r')\s*(?:\+|plus|and)\s*('+NUMBER+r')\s*('+unit_words+r')\s+(.+)$',clean(original),re.I)
    if added and not any(e.get('input')=='Meal Concierge editorial adaptation' for e in (i.get('evidence') or {}).values()):
        from recipes import source_ingredient
        parts=[recovered(source_ingredient(added[a]+' '+added[a+1]+' '+added[5])) for a in (1,3)]
        if all(p.get('scalable') and p.get('unit') in UNITS for p in parts):
            measures=[(read_quantity(p['quantity'])*UNITS[p['unit']][1],UNITS[p['unit']][0]) for p in parts]
            density=cup_weight(added[5])
            if len({u for _,u in measures})>1 and density is not None and {u for _,u in measures}<={'g','ml'}:
                measures=[(q*Fraction(str(density))/240 if u=='ml' else q,'g') for q,u in measures]
            if len({u for _,u in measures})==1:
                i.update(item=added[5],quantity=quantity_json(sum(q for q,_ in measures)),unit=measures[0][1],scalable=True)
                i['evidence']={k:estimate(original,'Added both stated quantities for the same ingredient, using the documented metric spoon/cup and ingredient-density conventions where necessary.') for k in ('quantity','unit')}
                return i
    if i.get('scalable') and i.get('unit') in UNITS:
        return i
    citrus_text=clean(original)
    citrus_text=re.sub(r'\bof half\b','of 1/2',citrus_text,flags=re.I)
    citrus_text=re.sub(r'\bof one\b','of 1',citrus_text,flags=re.I)
    citrus = re.match(r'^(?:juice|rind|peel|zest)(?:\s+(?:and|or)\s+(?:juice|rind|peel|zest))?\s+of\s+(' + NUMBER + r')\s+(.*)',citrus_text,re.I)
    if citrus and re.search(r'lemon|lime|orange',citrus[2],re.I):
        i.update(item=citrus[2][:240]+' for '+clean(original).split(' of ')[0].lower(), quantity=quantity_json(fraction(citrus[1])),unit='count',scalable=True)
        i['evidence']={k:source(original,'Retained the stated fruit count and the requested juice/zest preparation.') for k in ('quantity','unit')}
        return i
    explicit = i.get('amount') or ''
    if original.startswith('ingredient:'):
        columns=dict(re.findall(r'(ingredient|weight|volume|count):\s*([^;]*)',original))
        explicit=next((columns[k] for k in ('weight','volume','count') if columns.get(k) and re.search(r'\d|[¼½¾⅓⅔⅛⅜⅝⅞]',columns[k])),explicit)
    table_grams=False
    if original.startswith('ingredient:') and columns.get('weight')==explicit and re.fullmatch(NUMBER+r'(?:[–—-]'+NUMBER+r')?',clean(explicit)):
        explicit=clean(explicit)+' g';table_grams=True
    raw = clean(explicit if explicit else original)
    item = i['item'] if explicit else None
    assumptions = ['Interpreted a unitless numeric table weight in grams, consistent with the table’s metric weights.'] if table_grams else []
    # Source prose sometimes prefixes a numeric quantity with an approximation.
    if re.match(r'^at least\s+',raw,re.I):
        raw=re.sub(r'^at least\s+','',raw,flags=re.I)
        assumptions.append('Selected the explicitly stated lower bound as the base quantity; increase only as preparation requires.')
    raw = re.sub(r'^(?:heaping|heaped|a little over|scant|about|approximately|approx\.?|roughly|~)\s*', '', raw, flags=re.I)
    m = PREFIX.match(raw)
    if not m:
        return i
    n, tail = fraction(m[1]), m[3]
    tail = re.sub(r'^-\s*', '', tail)
    tail = re.sub(r'^a\s+(?=cup|tablespoon|teaspoon)', '', tail, flags=re.I)
    qualifier = re.match(r'^(?:level|heaped|heaping|generous|scant|rounded)\s+', tail, re.I)
    if qualifier:
        assumptions.append('Used a level measure for the stated spoon/cup quantity; source packing varies.')
        tail = tail[qualifier.end():]
    if m[2]:
        n = (n + fraction(m[2])) / 2
        assumptions.append('Selected the midpoint of the source range for the base batch; adjust to texture or taste while cooking.')
    # Parenthesized metric equivalents and package weights are source data.
    parent = re.match(r'^([^()\d]*)\(([^()]+)\)\s*(.*)', tail)
    if parent:
        measures = list(re.finditer(r'(' + NUMBER + r')\s*(kg|grams?|g|ml|litres?|liters?|l|ounces?|oz|pounds?|lb)\b', parent[2], re.I))
        if measures:
            chosen = next((part for part in measures if part[2].lower() in {'kg','g','gram','grams','ml','l','litres','liters'}), measures[0])
            unit = ALIASES.get(chosen[2].lower(), chosen[2].lower())
            q = fraction(chosen[1])
            comparable = [(part, ALIASES.get(part[2].lower(),part[2].lower())) for part in measures]
            masses = [(part, u) for part,u in comparable if u in UNITS and UNITS[u][0]=='g']
            if len(masses)>1:
                weights=[fraction(part[1])*UNITS[u][1] for part,u in masses]
                if max(weights)>min(weights)*2:
                    chosen,unit=masses[0];q=fraction(chosen[1]);assumptions.append('Conflicting printed mass equivalents: used the first stated mass and retained the discrepancy in original_text.')
            if re.search(r'can|tin|pack|bag|jar|bottle|box|tub|container|sachet',parent[1],re.I):
                if 'total' not in parent[2].lower():q*=n
                if n!=1 and 'total' not in parent[2].lower():assumptions.append('Interpreted the parenthesized package weight as the amount per package.')
            if re.search(r'\beach\b',parent[2],re.I) and not re.search(r'can|tin|pack|bag|jar|bottle|box|tub|container|sachet',parent[1],re.I):
                q*=n
            # Food names may precede the printed equivalent; a trailing
            # preparation phrase such as ', chopped' is not the ingredient.
            before=re.sub(r'^(?:cups?|sticks?|oz|ounces?|lb|pounds?|tbsp|tsp|tablespoons?|teaspoons?|cans?|tins?|packs?|packages?|bags?|jars?|bottles?)\b\s*(?:of\s+)?','',parent[1],flags=re.I).strip(' ,;')
            after=re.sub(r'^(?:of|cans?|tins?|packs?|packages?|bags?|jars?|bottles?)\b\s+', '',parent[3],flags=re.I).strip(' ,;')
            name = item or (before+', '+after if before and after else before or after)
            if name:
                i.update(item=name[:300],quantity=quantity_json(q),unit=unit,scalable=True)
                e=estimate(original,' '.join(assumptions)) if assumptions else source(original,'Used the source’s printed mass or volume equivalent.')
                i['evidence']={k:deepcopy(e) for k in ('quantity','unit')}
                return i
    multiplied=re.match(r'^x\s*(' + NUMBER + r')\s*(kg|g|ml|l)\b\s*(.*)',tail,re.I)
    if multiplied:
        name=item or multiplied[3]
        if name:
            i.update(item=name[:300],quantity=quantity_json(n*fraction(multiplied[1])),unit=multiplied[2].lower(),scalable=True)
            i['evidence']={k:source(original,'Multiplied the explicitly stated quantity and per-item measure.') for k in ('quantity','unit')}
            return i
    # A clearly stated per-package amount takes precedence over guessed sizes.
    package = re.match(r'(?:x\s*)?\(?(' + NUMBER + r')\s*(kg|g|ml|l|oz|ounces?)\s*\)?\s*(?:cans?|tins?|packs?|packages?|packets?|jars?|bottles?)\b\s*(.*)', tail, re.I)
    package2 = re.match(r'(?:cans?|tins?|packs?|packages?|packets?|jars?|bottles?)\s*\((' + NUMBER + r')\s*(kg|g|ml|l|oz|ounces?)\s*\)\s*(.*)', tail, re.I)
    pack = package or package2
    if pack:
        n *= fraction(pack[1]);unit = ALIASES.get(pack[2].lower(),pack[2].lower());tail=pack[3]
        item = item or re.sub(r'^of\s+', '', tail, flags=re.I)
        conversion = 'Multiplied source package count by its stated per-package amount.'
    else:
        conversion = None
        # Explicit printed metric equivalence: preserve it, including source rounding.
        metric = re.match(r'(?:cups?|oz|ounces?|lb|pounds?|tbsp|tsp|tablespoons?|teaspoons?)\s*\((' + NUMBER + r')\s*(g|kg|ml|l)(?:\s*/[^)]*)?\)\s*(.*)',tail,re.I)
        if metric:
            n, unit, tail = fraction(metric[1]),metric[2].lower(),metric[3]
            item = item or tail
            conversion = 'Used the metric equivalent printed for this source ingredient.'
        else:
            units = sorted(set(UNITS)|set(ALIASES)|{'cups','cup','fl oz','fluid ounces','fluid ounce','pint','pints','quart','quarts','gallon','gallons'},key=len,reverse=True)
            um = next((m for u in units if (m := re.match(re.escape(u)+r'(?![^\W\d_])\.?\s*(.*)',tail,re.I))),None)
            if um:
                text_unit = tail[:len(tail)-len(um[1])].strip(' .').lower()
                unit = ALIASES.get(text_unit,text_unit);tail=um[1]
                item=item or re.sub(r'^of\s+','',tail,flags=re.I)
                if unit in {'cup','cups','fl oz','fluid ounces','fluid ounce','pint','pints','quart','quarts','gallon','gallons'}:
                    volume={'cup':240,'cups':240,'fl oz':30,'fluid ounces':30,'fluid ounce':30,'pint':480,'pints':480,'quart':960,'quarts':960,'gallon':3840,'gallons':3840}[unit]
                    weight=cup_weight(item)
                    if weight is not None and unit in {'cup','cups'}:
                        n *= weight;unit='g';assumptions.append(f'Project cooking estimate: one level 240 ml cup of this ingredient weighs approximately {weight} g; packing and brands vary.')
                    else:
                        n *= volume;unit='ml';assumptions.append(f'Project volume convention: each source measure is treated as {volume} ml; its locale was not stated.')
                elif unit in {'tsp','tbsp','teaspoon','teaspoons','tablespoon','tablespoons'}:
                    assumptions.append('Project metric spoon convention: teaspoon=5 ml and tablespoon=15 ml.')
            else:
                cm=re.match(r'(dozen|cloves?|slices?|pieces?|sprigs?|leaves|leaf|heads?|stalks?|stems?|strips?|sheets?|fillets?|fillets|breasts?|legs?|thighs?|wings?|ea\s*\.?|whole)\b\.?\s*(.*)',tail,re.I)
                if cm:
                    noun,tail=cm[1].lower(),cm[2]
                    if noun=='dozen':n*=12
                    item=item or re.sub(r'^of\s+','',tail,flags=re.I)
                    if noun not in {'dozen','ea','whole','piece','pieces'} and noun not in item.lower():item=f'{item} ({noun})'
                    unit='count'
                elif re.search(r'\b(?:eggs?|yolks?|whites|onions?|shallots?|carrots?|potatoes|tomatoes|tomato|leeks?|lemons?|limes?|oranges?|apples?|pears?|bananas?|chillies|chiles|peppers?|jalape[ñn]os?|garlic|cucumbers?|aubergines?|eggplants?|zucchini|courgettes?|avocados?|tortillas?|fillets?|steaks?|breasts?|thighs?|wings?|drumsticks?|sausages?|frankfurters|cubes?|loaves|baguettes?|muffins?|sandwiches|bagels?|rolls?|buns?|crackers?|cookies?|biscuits?|sheets?|stalks?|sticks?|pods?|seeds|threads|grains|leaves|berries|dates|prunes|cherries|walnuts?|almonds?|cloves|nutmeg|spring onions?|chilli|scotch bonnet|leaf|bay leaf|celery|cabbage|lettuce|cardamom|star anise|bacon|chorizo|bread|pita|mushrooms?|scallions?|potato|chops|chicken|pork|lamb|veal|roast|slabs?|wingettes|prawns|oysters|scallops|squid|mackerel|apricots?|peaches?|plantain|coconut|breadfruit|fennel|squash|beetroot|turnips?|celeriac|bouquet garni|peppercorns|thyme|rosemary|mint|cashew|tamarind|brussels sprouts|galangal|lemongrass|mozzarella|nutmeg|wrappers?)\b', item or tail,re.I):
                    item=item or tail
                    # Package/part words require a subsequent explicit editorial choice.
                    if re.match(r'(?:(?:small|large|a|thin|thick)\s+)?(?:%|(?:US|imperial|metric)\s|or so\s|(?:dessert|cooking)\s|spoon(?:ful)?s?\s|shakes?\s|(?:small|large)\s+(?:spoon|cup)|cups?|cans?|tins?|packs?|packages?|packets?|jars?|bottles?|parts?|pinch|pinches|dash|dashes|handful|handfuls|bunch|bunches|stick|sticks|bags?|scoops?|drops?|knobs?|dollops?|inch|inches|cm)\b',tail,re.I):return i
                    unit='count';assumptions.append('Interpreted the source number as individual medium-sized items; size and edible yield vary.')
                else:return i
    if not item or n<=0:return i
    item=item.strip(' ,;')
    i.update(item=item[:300],quantity=quantity_json(n),unit=unit,scalable=True)
    evidence=estimate(original,' '.join(assumptions)) if assumptions else source(original,conversion)
    i['evidence']={'quantity':deepcopy(evidence),'unit':deepcopy(evidence)}
    return i


def batch_mass(ingredient):
    if not ingredient.get('quantity') or ingredient.get('unit') not in UNITS:return 0
    n= float(read_quantity(ingredient['quantity'])); unit,factor=UNITS[ingredient['unit']];n*=float(factor)
    if unit in {'g','ml'}:return n
    item=re.split(r'\s+or\s+',ingredient['item'].casefold())[0].split(',')[0]
    size=re.search(r'(\d+(?:\.\d+)?)\s*(kg|g|lb|pound|oz)\b', item)
    if size:
        return n*float(size[1])*{'kg':1000,'g':1,'lb':453.59237,'pound':453.59237,'oz':28.349523125}[size[2]]
    for pattern, grams in [(r'(?:king |jumbo )?(?:prawns?|shrimps?)',20),(r'cherry tomatoes?|grape tomatoes?',15),(r'eggs? whites?|egg yolks?',25),(r'spring onions?|scallions?',15),(r'garlic.*head|head.*garlic',50),(r'stock cube|seasoning cube',10),(r'garlic',5),(r'cardamom|peppercorn|whole cloves?|^\(?cloves?\)?$',.2),(r'cinnamon stick',3),(r'chillies|chilli|chiles|chili|scotch bonnet',10),(r'asparagus',20),(r'falafel',25),(r'shallots?',30),(r'wingettes?|wings?',45),(r'drumsticks?|legs?',250),(r'boneless pork loin roast|slab|roast',1200),(r'strawberr',15),(r'grapes?|cherries',5),(r'pecan halves|almonds?|hazelnuts?|cashews?',2),(r'walnuts?|pecans?|chestnuts?',5),(r'cloves?|garlic',5),(r'eggs?|yolks?|whites',50),(r'leaf|leaves|sprigs?|thyme|rosemary|mint|basil|parsley|coriander|cilantro',1),(r'star anise',1),(r'slices?|strips?',25),(r'breasts?|fillets?|steaks?',175),(r'whole chicken',1500),(r'chicken',200),(r'lemon|lime',70),(r'potato|tomato|onion|apple|pear',150),(r'carrot|banana',100),(r'bell pepper',150),(r'tortilla|bread|bun|roll|muffin',60),(r'head.*cabbage',800)]:
        if re.search(pattern,item):return n*grams
    return n*100


def serving_estimate(recipe):
    name=re.split(r'\s+with\s+',recipe['name'].casefold())[0]; masses={}
    for i in recipe['ingredients']:
        g=group(i['item'])
        if re.search(r'\b(?:water|ice)\b',i['item'],re.I) or (g=='fat' and batch_mass(i)>=500 and re.search(r'fry|frying', ' '.join(recipe['steps']),re.I)):
            continue
        masses[g]=masses.get(g,0)+batch_mass(i)
    total=sum(masses.values());y=recipe.get('yield') or {};yield_text=re.split(r'\s+OR\s+',clean(y.get('original_text') or ''),flags=re.I)[0]
    m=PREFIX.match(re.sub(r'^(?:makes?|about|approximately|enough for)\s+','',yield_text,flags=re.I))
    if m and not re.match(r'(?:inch|cm|mm|centimet)',m[3],re.I):
        n=fraction(m[1]);n=(n+fraction(m[2]))/2 if m[2] else n;u=m[3].lower()
        if 'dozen' in u:n*=12
        per=next((v for p,v in [(r'cookie|biscuit|wonton|dumpling|blini|kal kal|tamale',3),(r'loaf|loaves',8),(r'cake|pie|pizza',8),(r'bread|flatbread|gözleme',2),(r'roll|muffin|bun|sandwich|slice|burger',1)] if re.search(p,u)),None)
        if per:
            whole=bool(re.search(r'\b(?:loaf|loaves|cakes?|pies?|pizzas?)\b',u))
            if re.search(r'\bcakes?\b',u) and total/float(n)<300:
                whole=False;per=1
            if re.search(r'\bpizzas?\b',u):per=2
            count=max(1,round(float(n)*per if whole else float(n)/per))
            return count,f'Estimated {count} person servings from source yield "{yield_text}" using {per} '+('servings per whole item.' if whole else 'pieces per serving.')
    if re.search(r'soup|broth|congee|porridge|drink|juice|lemonade|tea\b|coffee|soda|milkshake|smoothie',name):
        retained=sum(batch_mass(i) for i in recipe['ingredients'] if re.search(r'\bwater\b',i['item'],re.I))
        total+=retained
    dessert=('dessert' in {str(t).casefold() for t in recipe.get('tags',[])}) or bool(re.search(r'torte|torta|taart|chajá|rugelach|baklava|galaktoboureko|halwa|laddu|kulfi|phirni|kheer|gulab|barfi|burfi',name))
    if dessert:target,role=100,'dessert portion'
    elif re.search(r'shepherd|cottage pie|pot pie|meat pie|vegetable.*pie',name):target,role=350,'meal or side portion'
    elif re.search(r'soup|broth|congee|porridge',name):target,role=400,'soup portion'
    elif masses.get('protein',0)>=300:target,role=350,'meal or side portion'
    elif re.search(r'cocktail|martini|margarita|mojito|daiquiri|drink|smoothie|milkshake|lemonade|tea\b|coffee|sima|soda|beer|juice',name): target,role=250,'drink'
    elif re.search(r'^(?:leaf masala|spice blend|spice mix|seasoning|baking mix)$|(?:rub|seasoning|spice mix)$',name):target,role=10,'seasoning portion'
    elif re.search(r'cookie|biscuit|cake|brownie|muffin|tart|pie|pudding|sweet|dessert|fudge|candy|chocolate|truffle|halva|buns|balls|panna cotta',name):target,role=100,'dessert portion'
    elif re.search(r'bread|dough|rolls|flatbread|crêpe|crepe|pancake|waffle',name):target,role=100,'bread or pancake portion'
    elif re.search(r'jam|jelly|chutney|ketchup|mayonnaise|mustard|pesto|sauce|syrup|dressing|butter|marmalade|paste',name) and masses.get('protein',0)<200 and masses.get('vegetable',0)<500 and not re.search(r'rice|sandwich|pita|panna cotta',name):target,role=30,'condiment portion'
    elif re.search(r'cookie|biscuit|cake|brownie|muffin|tart|pie|pudding|sweet|dessert|fudge|candy|chocolate|truffle|halva',name):target,role=100,'dessert portion'
    elif re.search(r'bread|dough|rolls|flatbread|crêpe|crepe|pancake|waffle',name):target,role=100,'bread or pancake portion'
    elif re.search(r'soup|broth',name):target,role=400,'soup portion'
    else:target,role=350,'meal or side portion'
    # Cooked grains/legumes include absorbed water; use their dry equivalent
    # only for the main-food serving comparison, not for total batch mass.
    dry_carb=sum(batch_mass(i)*(0.4 if re.search(r'\b(?:cooked|canned|drained|steamed|tinned)\b', i['item'], re.I) else 1) for i in recipe['ingredients'] if group(i['item'])=='carb')
    if role=='meal or side portion' and masses.get('protein',0)>=150:
        count=max(1,round(max(total/350,masses['protein']/180,(dry_carb/85),masses.get('vegetable',0)/250)))
    elif role=='meal or side portion' and masses.get('carb',0)>=100:
        count=max(1,round(max(total/350,dry_carb/90)))
    else:count=max(1,round(total/target))
    if total<100:count=2 if role in {'meal or side portion','soup portion'} else 1
    return count,f'Project estimate: {count} person servings as {role}s, based on declared ingredient amounts (approximately {round(total)} g/ml before cooking), typical item sizes and a {target} g/ml serving reference. This is an editable culinary estimate, not source yield or a nutritional calculation.'


def assumed_amount(ingredient, portions, recipe):
    i=deepcopy(ingredient);original=i.get('original_text') or i['raw'];item=i['item'];text=clean(original).lower();g=group(item)
    m=PREFIX.match(text);count=fraction(m[1]) if m else Fraction(1);tail=m[3] if m else text
    if m and m[2]:count=(count+fraction(m[2]))/2
    if original.startswith('ingredient:'):
        columns=dict(re.findall(r'(weight|volume|count):\s*([^;]*)',original))
        explicit=next((columns[k] for k in ('weight','volume','count') if columns.get(k) and re.search(r'\d|[¼½¾⅓⅔⅛⅜⅝⅞]',columns[k])),None)
        if explicit:
            text=clean(explicit).lower()+' '+item.lower();m=PREFIX.match(text);count=fraction(m[1]) if m else Fraction(1);tail=m[3] if m else text
            if m and m[2]:count=(count+fraction(m[2]))/2
    note=None;unit='g';amount=None
    if not m and re.search(r'dust(?:ing)?|sprinkl|sprink|coat(?:ing)?|greas(?:e|ing)',text) and g in {'carb','bread','fat','sugar'}:
        amount=Fraction(min(60,max(10,portions*2)));unit='ml' if g=='fat' and 'oil' in text else 'g';note='Selected a modest dusting/coating allowance for this batch; use only what adheres.'
    elif not m and re.search(r'garnish|decorat|to serve|for serving|a few|small amount|a little|drizzle|slices of|slivers',text):
        size={'herb':1,'spice':.25,'aromatic':2,'sauce':10,'sugar':5,'fat':3,'liquid':15,'fruit':15,'vegetable':20,'protein':25,'carb':10,'bread':10,'nuts':5,'dairy':15,'chocolate':5,'egg':.25}.get(g,10)
        amount=Fraction(str(min(500,max(2,portions*size))));unit='ml' if g in {'liquid','sauce'} or g=='fat' and 'oil' in text else 'g';note='Selected a garnish/finishing allowance rather than a main-food portion.'
    elif re.search(r'egg wash',text):amount=Fraction(1 if portions<=16 else 2);unit='count';note='Selected one egg for brushing a small batch, two for a large batch; use only the coating needed.'
    elif re.search(r'\b(?:stock|bouillon|seasoning) cubes?\b',text):amount=count;unit='count';note='Selected one stock/seasoning cube per stated unit; dilute or season according to its package.'
    elif re.search(r'\bbay lea(?:f|ves)\b',text):amount=count if m else Fraction(max(1,min(4,round(portions/4))));unit='count';note='Selected whole bay leaves for the cooking batch; remove before serving.'
    elif re.search(r'vanilla.*sugar|sugar.*vanilla',text) and re.search(r'pack|sachet|bag',tail):amount=count*8;note='Selected an 8 g vanilla-sugar packet for the source flavoring packet.'
    elif re.search(r'zest|lemon peel|lime peel|orange peel',text) and not re.search(r'candied',text):amount=Fraction(portions);note='Estimated fresh citrus zest/peel at 1 g per serving; this is peel, not whole fruit.'
    elif re.search(r'\bpinch(?:es)?\b',tail):amount=count*Fraction(5,16);unit='ml';note='A pinch is estimated as 1/16 metric teaspoon.'
    elif re.search(r'\bdash(?:es)?\b',tail):amount=count*Fraction(5,8);unit='ml';note='A dash is estimated as 1/8 metric teaspoon.'
    elif re.search(r'\bdrops?\b',tail):amount=count*Fraction(1,20);unit='ml';note='A drop is estimated as 0.05 ml.'
    elif re.search(r'\bhandfuls?\b',tail):amount=count*(5 if g=='herb' else 30);note='A handful is estimated as 5 g for herbs or 30 g for other ingredients.'
    elif re.search(r'\b(?:bunch|bunches)\b',tail):amount=count*(30 if g in {'herb','aromatic'} else 250);note='A bunch is estimated as 30 g for herbs/aromatics or 250 g for vegetables.'
    elif re.search(r'\b(?:stick|sticks)\b',tail) and g=='fat':amount=count*113;note='A butter stick is estimated as 113 g.'
    elif re.search(r'\b(?:cans?|tins?|packs?|packages?|packets?|jars?|bottles?|bags?)\b',tail):
        size={'carb':400,'vegetable':400,'fruit':400,'protein':200,'dairy':200,'sauce':200,'sugar':350,'spice':7,'herb':20,'bread':250,'fat':250,'liquid':330,'aromatic':400,'nuts':100,'chocolate':100}.get(g)
        if size:amount=count*size;unit='ml' if g=='liquid' else 'g';note=f'Unspecified package size estimated at {size} {unit}; select a package that covers this amount.'
    elif re.match(r'(?:parts?|volumes?)\b',tail):
        part_rows=[row for row in recipe['ingredients'] if re.match(r'^'+NUMBER+r'\s+(?:parts?|volumes?)\b',clean(row.get('original_text') or row['raw']),re.I)]
        weight_basis=any('weight' in (row.get('original_text') or '') for row in part_rows) or not all(group(row['item']) in {'liquid','sauce','sugar','fat'} for row in part_rows)
        amount=count*50;unit='g' if weight_basis else 'ml';note='Defined one part as 50 '+unit+' consistently across this proportional batch.'
    elif re.search(r'\bpunnet\b',tail):amount=count*125;note='Selected a 125 g berry punnet for the base batch.'
    elif re.search(r'\bshakes?\b',tail):amount=count*Fraction(1,4);note='Estimated one seasoning-shaker shake as 0.25 g.'
    elif re.search(r'\b(?:knob|dollop|splash|dab|scoop)s?\b',tail):amount=count*(15 if g not in {'carb','protein','vegetable'} else 50);unit='ml' if g in {'liquid','sauce','fat'} else 'g';note='An unspecified spoonful/knob/splash is estimated for the base batch.'
    elif g and m:
        # A remaining number has no usable standard measure. Preserve its
        # multiplier and explicitly choose the culinary form/size below.
        length=re.match(r'-?\s*(cm|mm|inch(?:es)?|in)\b',tail,re.I)
        if length and re.search(r'ginger|galangal|turmeric|cinnamon',text):
            amount=count*({'cm':5,'mm':0.5,'inch':12,'inches':12,'in':12}[length[1].lower()]);amount=Fraction(str(amount));note='Estimated root/stick weight from the stated length: 5 g per cm (12 g per inch); diameter varies.'
        elif length and re.search(r'crust|shell',text):
            amount=Fraction(1);unit='count';note='The leading length describes one pie crust diameter, not the number of crusts.'
        elif re.match(r'(?:%|[- ]?spice|HBU|AAU)',tail,re.I):return i,False
        elif re.search(r'\b(?:spoon|spoons|spoonful|spoonfuls|soupspoons|dessert spoons?)\b',tail):
            amount=count*(10 if 'dessert' in tail else 15);unit='ml';note='An unspecified cooking spoon is estimated as 15 ml; a dessert spoon as 10 ml.'
        elif re.match(r'(?:glass(?:es)?|teacup|wineglass|shot|shots|measures?)\b',tail):
            amount=count*(30 if re.match(r'shot|measure',tail) else 200);unit='ml';note='Estimated drinking measure: glass=200 ml or shot=30 ml.'
        elif re.match(r'(?:volume|volumes)\b',tail):
            amount=count*50;unit='ml';note='Defined one proportional volume as 50 ml for this base batch.'
        elif re.search(r'\b(?:box|boxes|container|carton|tub|tubs|pot|block|sachet|pkg|nylons|wrappings)\b',tail):
            amount=count*({'carb':400,'vegetable':400,'fruit':250,'protein':250,'dairy':250,'sauce':200,'sugar':250,'spice':7,'herb':20,'bread':250,'fat':250,'liquid':500,'aromatic':400,'nuts':100,'chocolate':100}.get(g,100));note='Selected a typical package amount for the base batch; the source gives no usable package size.';unit='ml' if g=='liquid' else 'g'
        elif g in {'fruit','vegetable','egg','protein','nuts','bread'} or re.search(r'\b(?:leaf|leaves|sticks?|pods?|sprigs?|threads|blades?|strands|bouquet)\b',tail):
            amount=count;unit='count';note='Interpreted the source number as individual items, slices, pieces or whole produce as described; size varies.'
        else:
            size={'spice':5,'herb':2,'aromatic':10,'fat':15,'liquid':240,'sauce':15,'sugar':15,'dairy':30,'carb':120,'chocolate':25}[g]
            amount=count*size;unit='ml' if g in {'fat','liquid','sauce'} else 'g';note=f'Unspecified source measure interpreted as {size} {unit} per stated unit for this ingredient, retaining the numeric multiplier.'
    elif g and not m:
        if g=='spice':
            batch=sum(batch_mass(row) for row in recipe['ingredients'])
            seasoning_portions=min(portions,max(1,round(batch/350))) if batch else portions
            amount=Fraction(seasoning_portions,2) if 'salt' in text else Fraction(seasoning_portions,4)
        elif g=='herb':amount=Fraction(min(30,portions*2))
        elif g=='aromatic':amount=Fraction(min(60,portions*5))
        elif g=='fat':
            amount=Fraction(1000 if re.search(r'deep.?fry',text+' '+' '.join(recipe['steps']).lower()) else portions*5);unit='ml' if 'oil' in text or 'spray' in text else 'g'
        elif g=='liquid':
            unit='ml';amount=Fraction(portions*(250 if 'water' in text or 'stock' in text or 'broth' in text else 50))
            if re.search(r'lemon|lime',text):amount=Fraction(min(150,portions*10))
            if re.search(r'rum|brandy|liqueur|whisk|cognac',text) and re.search(r'cake|dessert|tart|pudding|cookie',recipe['name'],re.I):amount=Fraction(min(120,portions*5))
            if re.search(r'\bice\b',text):amount=Fraction(min(1000,portions*50))
            flour=sum(batch_mass(row) for row in recipe['ingredients'] if re.search(r'flour',row['item'],re.I))
            rice=sum(batch_mass(row) for row in recipe['ingredients'] if re.search(r'\brice\b',row['item'],re.I) and not re.search(r'cooked',row['item'],re.I))
            if 'water' in text and flour and re.search(r'bread|dough|flatbread|bun|roll|biscuit|pastry|crust',recipe['name'],re.I):
                other=sum(batch_mass(row) for row in recipe['ingredients'] if re.search(r'milk|water',row['item'],re.I))
                amount=Fraction(str(max(30,round(flour*(.4 if re.search(r'pastry|crust',recipe['name'],re.I) else .65)-other*.85))))
            elif 'water' in text and rice:amount=Fraction(str(round(rice*2)))
        elif g=='sauce':amount=Fraction(portions*(2 if 'extract' in text or 'vanilla' in text else 5 if 'vinegar' in text else 15));unit='ml'
        elif g=='sugar':amount=Fraction(portions*10)
        elif g=='egg':amount=Fraction(max(1,round(portions/2)));unit='count'
        elif g=='carb' and re.search(r'flour',text) and re.search(r'soup|sauce|gravy',recipe['name'],re.I):amount=Fraction(portions*8)
        elif g=='carb' and re.search(r'flour',text) and re.search(r'bread|dough|pancake|batter|fritter|cake|cookie',recipe['name'],re.I):
            liquid=sum(batch_mass(row) for row in recipe['ingredients'] if re.search(r'water|milk',row['item'],re.I))
            amount=Fraction(str(max(50,round(liquid/(1 if re.search(r'pancake|batter',recipe['name'],re.I) else .65))))) if liquid else Fraction(portions*75)
        else:
            size={'dairy':30,'protein':150,'carb':75,'bread':50,'nuts':20,'fruit':100,'vegetable':100,'chocolate':20}[g]
            if ingredient.get('optional'):size=min(size,30)
            if re.search(r'dried.*(?:shrimp|crayfish)|ground.*(?:crayfish|shrimp)',text):size=5
            if re.search(r'olives?|raisins?|maraschino',text):size=10
            if g in {'protein','vegetable','fruit'}:
                peers=sum(1 for row in recipe['ingredients'] if group(row['item'])==g and not row.get('scalable'))
                size/=max(1,peers)
                if g=='protein' and sum(batch_mass(row) for row in recipe['ingredients'] if group(row['item'])=='protein')>=portions*100:size=min(size,30)
            amount=Fraction(str(round(portions*size,2)))
        note=f'Project cooking allowance for {portions} estimated/source person servings; source did not provide a usable amount. Adjust seasoning, liquid or texture during preparation.'
    if amount is None:return i,False
    if m:
        item=re.sub(r'^'+re.escape(text[:len(text)-len(m[3])])+r'\s*','',clean(item),flags=re.I)
        item=re.sub(r'^(?:pinches?|dash(?:es)?|handfuls?|bunch(?:es)?|cans?|tins?|packs?|packages?|packets?|jars?|bottles?|bags?|parts?|sticks?|drops?|knobs?|dollops?|splashes?|scoops?)\b\s*(?:of\s+)?','',item,flags=re.I) or i['item']
    i.update(item=item[:300],quantity=quantity_json(amount),unit=unit,scalable=True)
    i['evidence']={k:estimate(original,note) for k in ('quantity','unit')}
    return i,True



def editorial_rows(recipe):
    """Separate source headings/equipment from edible ingredients and choose variants."""
    from recipes import source_ingredient
    rows=[];notes=[]
    for old in recipe['ingredients']:
        text=old.get('original_text') or old['raw'];item=old['item']
        if re.search(r'^(?:↑|The bread and the filling|The cake and the topping|For the dipping sauce:|Quick Salsa:|Syrup:|For (?:each litre|[~\d¼½¾]|garnish)|Serves \d|Use any of the following|Many different ingredients|This recipe comes from|You can add whatever|Choose \d|Depending on taste|The quantities below|The recipe is easy|There are five|When preparing Abacha|Either:|or:|Main Ingredients$|Spice Mix$|Flavor solution [AB]:|Topping$)',text,re.I):
            notes.append('Source heading/context: '+text);continue
        if re.search(r'\b(?:wood(?:en)?|hickory|mesquite|applewood|maple plank|skewers|toothpicks|nylon bags|molds?|moulds|wrapping|moi.moi leaves)\b',text,re.I) and not re.search(r'smoke|syrup|sauce|powder|liquid',text,re.I):
            notes.append('Equipment/fuel required: '+text);continue
        if old.get("scalable"):
            rows.append(old)
            continue
        # Explicit numeric alternatives/additions need one selected branch or
        # separate rows, not an ingredient query containing multiple recipes.
        def split_outside(value, pattern):
            cuts=[];depth=0
            for match in re.finditer(pattern,value,flags=re.I):
                prefix=value[:match.start()]
                depth=prefix.count('(')-prefix.count(')')
                if depth==0:cuts.append((match.start(),match.end()))
            parts=[];last=0
            for begin,finish in cuts:parts.append(value[last:begin]);last=finish
            return parts+[value[last:]]
        alternatives=split_outside(text,r'\s+or\s+(?=\d)')
        chosen=alternatives[0]
        parts=split_outside(chosen,r'\s+(?:and|plus)\s+(?=\d)')
        if (len(parts)>1 or len(alternatives)>1) and all(group(part) for part in parts):
            for part in parts:
                new=source_ingredient(part)
                new['notes']='Meal Concierge selected the first numeric alternative and separated explicitly additive amounts. Original wording: '+text[:300]
                rows.append(new)
            notes.append('Selected ingredient branch: '+chosen)
        else:rows.append(old)
    if notes:recipe['notes']='\n'.join(filter(None,[recipe.get('notes'),'Meal Concierge extraction decisions: '+'; '.join(notes)]))
    recipe['ingredients']=rows
    return recipe


def apply_amendment(recipe, credit, amendments):
    from recipes import normalize_recipe
    identity=recipe['source']['kind']+':'+recipe['source']['external_id']
    amendment=(amendments or {}).get(identity)
    if amendment is None:return recipe,credit
    if not isinstance(amendment,dict):raise ValueError('editorial amendment must be an object')
    if amendment.get('source_hash')!=recipe['external_snapshot']['content_hash']:
        raise ValueError('editorial amendment does not match the sealed recipe source')
    if set(amendment)=={'source_hash','exclude_reason'} and amendment['exclude_reason']=='missing_source_method':
        raise SourceMethodExcluded('Source has no actionable preparation method; omitted by publisher policy.')
    if set(amendment)-{'source_hash','note','set','resolved_issues','omit_cover'} or ('omit_cover' in amendment and amendment['omit_cover'] is not True):
        raise ValueError('editorial amendment does not match the sealed recipe source')
    changes=amendment.get('set',{})
    if set(changes)-{'name','ingredients','steps','portions','portions_evidence','yield','notes'}:
        raise ValueError('editorial amendment cannot replace source identity, attribution or cover')
    if 'yield' in changes and recipe.get('yield'):
        credit['original_yield']=deepcopy(recipe['yield'])
    recipe.update(deepcopy(changes))
    recipe['notes']='Meal Concierge editorial adaptation: '+amendment['note']
    if set(amendment.get('resolved_issues',[]))!=set(credit.get('normalization_issues',[])):
        raise ValueError('editorial resolution must account for every current source issue')
    credit['resolved_normalization_issues']=credit.pop('normalization_issues',[])
    credit['editorial_adaptation']=amendment['note']
    if amendment.get('omit_cover'):
        credit['image_omission']='Original cover no longer represents the adapted recipe.'
    return normalize_recipe(recipe),credit

def curate(recipe, credit, *, pack_version, amendments=None):
    from recipes import normalize_recipe, recipe_evidence_fields
    recipe=deepcopy(recipe);credit=deepcopy(credit)
    from recipe_pack_sources import readiness
    identity=recipe['source']['kind']+':'+recipe['source']['external_id']
    if identity not in (amendments or {}) and not credit.get('normalization_issues') and readiness(recipe)[0]=='ready':
        return recipe,credit
    recipe,credit=apply_amendment(recipe,credit,amendments)
    recipe=editorial_rows(recipe)
    recipe['ingredients']=[recovered(i) for i in recipe['ingredients']]
    for index,i in enumerate(recipe['ingredients']):
        if not i.get('scalable') and PREFIX.match(clean(i.get('original_text') or i['raw'])):
            recipe['ingredients'][index],_ = assumed_amount(i,1,recipe)
    if recipe.get('portions') is None:
        portions,note=serving_estimate(recipe);recipe['portions']=portions
        recipe['portions_evidence']=estimate((recipe.get('yield') or {}).get('original_text'),note)
    portions=round(float(read_quantity(recipe['portions']))) or 1
    unresolved=[]
    completed=deepcopy(recipe['ingredients'])
    for index,i in enumerate(recipe['ingredients']):
        if not i.get('scalable'):
            completed[index],done=assumed_amount(i,portions,recipe)
            if not done:unresolved.append(f'ingredients.{index}.unhandled_food')
    recipe['ingredients']=completed
    # Project review is introduced only by the offline publisher step. Runtime
    # external boundaries reject copied or forged markers.
    for evidence in recipe_evidence_fields(recipe).values():
        if evidence.get('basis')=='estimate' and evidence.get('assumptions'):
            evidence['project_review']={'publisher':'Meal Concierge','pack_id':'wikibooks-themealdb-en','pack_version':pack_version}
    recipe['external_snapshot']['changes']='Structured by Meal Concierge; original source text retained separately. Quantities and person servings may include explicitly marked project estimates and editorial adaptations.'
    credit['curation']={'policy':POLICY,'pack_version':pack_version,'unresolved':unresolved}
    return normalize_recipe(recipe),credit
