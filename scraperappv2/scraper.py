import os
import re
import shutil
import zipfile
import threading
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import psutil  # Added for memory monitoring

# PLAYWRIGHT SETUP 
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logging.warning("Playwright not installed. Dynamic scraping will be disabled. To enable, run: pip install playwright && playwright install chromium")

# CONFIGURATION 
CSS_URL_RE = re.compile(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", re.IGNORECASE)
DEFAULT_USER_AGENT = "MirrorBot/2.0 (AdvancedConverter)"
DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_WORKERS = 10
REQUEST_DELAY = 0.1
REQUEST_TIMEOUT = 20
DYNAMIC_SCRAPE_TIMEOUT = 30
DYNAMIC_SCRAPE_THRESHOLD = 500
# --- FIX: This reliably finds the project's root directory and sets the output folder there. ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "mirror_upgraded"
lock = threading.Lock()

# LOGGING SETUP 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# GEMINI API INTEGRATION
def call_gemini_api(payload: dict) -> dict | None:
    api_key = "AIzaSyBckkt1iIpN6sU7uNrD19wp8KNOYxu2qqQ"
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    # Memory check before API call
    mem = psutil.virtual_memory()
    if mem.percent > 90:  # Abort if memory is too high
        logger.error("Memory usage too high (90%+), aborting API call")
        return None
        
    for attempt in range(3):  # Retry up to 3 times
        try:
            response = requests.post(api_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=120)
            response.raise_for_status()
            data = response.json()
            if 'candidates' in data and data['candidates']:
                part = data['candidates'][0]['content']['parts'][0]
                json_string = part.get('text', '')
                if json_string.strip().startswith("```json"):
                    json_string = json_string.strip()[7:-3]
                return json.loads(json_string)
            logger.error(f"Unexpected API response format: {data}")
        except requests.exceptions.Timeout:
            logger.warning(f"API timeout (attempt {attempt+1}/3)")
            time.sleep(5)  # Wait before retrying
        except requests.exceptions.RequestException as e:
            logger.error(f"Gemini API request failed: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON from Gemini API response: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during Gemini API call: {e}")
        
        # Wait before next attempt
        time.sleep(2)
        
    return None

def log_memory_usage():
    """Log current memory usage"""
    mem = psutil.virtual_memory()
    logger.info(f"Memory usage: {mem.used/(1024*1024):.2f}MB / {mem.total/(1024*1024):.2f}MB ({mem.percent}%)")

def decompose_html_with_ai(html_content: str, css_content: str) -> dict | None:
    logger.info("Decomposing HTML into logical components with AI...")
    prompt = f"""
    You are a senior front-end architect. Your task is to analyze the following HTML and CSS, and decompose the HTML into a structured JSON object of logical, reusable components.
    **Instructions:**
    1.  Identify distinct, self-contained sections of the HTML (e.g., header, hero, feature-card, footer).
    2.  For each section, create a key in the JSON object. The key should be a valid, capitalized component name in PascalCase (e.g., "MainHeader", "ProductGrid").
    3.  The value for each key should be the corresponding full HTML snippet for that component.
    4.  The final output must be ONLY the JSON object.
    **HTML to Analyze:**
    ```html
    {html_content}
    ```
    **Associated CSS for Context:**
    ```css
    {css_content}
    ```
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}
    return call_gemini_api(payload)

def convert_html_snippet_to_component(html_snippet: str, css_content: str, component_name: str) -> dict | None:
    logger.info(f"Converting snippet to React component: {component_name}...")
    prompt = f"""
    You are an expert React developer specializing in Tailwind CSS. Convert the provided HTML snippet and its full CSS context into a single, self-contained React JSX component.

    **Instructions:**
    1.  The component name must be `{component_name}`.
    2.  Convert the HTML to valid JSX, ensuring all tags are closed and attributes like `class` are changed to `className`.
    3.  Analyze the provided CSS and apply the equivalent Tailwind CSS utility classes directly to the `className` attributes of the JSX elements.
    4.  The final output must be a single functional React component. **Do not include `import React from 'react';` or a default export.**
    5.  Return the result as a JSON object with a single key 'react_component'.

    **HTML Snippet:**
    ```html
    {html_snippet}
    ```

    **Full CSS Context:**
    ```css
    {css_content}
    ```
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": {"type": "OBJECT", "properties": {"react_component": {"type": "STRING"}}, "required": ["react_component"]}}}
    return call_gemini_api(payload)

def convert_css_to_tailwind(css_content: str) -> dict | None:
    logger.info("Converting CSS to Tailwind with Gemini...")
    prompt = f"Analyze the following CSS code. Convert it into a single string of Tailwind CSS utility classes. Return as a JSON object with a single key 'tailwind_classes'.\n\nCSS:\n```css\n{css_content}\n```"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": {"type": "OBJECT", "properties": {"tailwind_classes": {"type": "STRING"}}, "required": ["tailwind_classes"]}}}
    return call_gemini_api(payload)

# --- SCRAPING & FILE HANDLING ---
def setup_playwright_browser():
    if not PLAYWRIGHT_AVAILABLE: 
        return None, None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
        return browser, context
    except Exception as e:
        logger.error(f"Failed to set up Playwright. Error: {e}")
        return None, None

def fetch_with_playwright(url: str, context) -> str | None:
    try:
        page = context.new_page()
        page.goto(url, timeout=DYNAMIC_SCRAPE_TIMEOUT * 1000)
        page.wait_for_load_state("networkidle", timeout=DYNAMIC_SCRAPE_TIMEOUT * 1000)
        time.sleep(3)
        content = page.content()
        page.close()
        return content
    except Exception as e:
        logger.error(f"Playwright failed to fetch {url}: {e}")
        return None

def fetch_static(session: requests.Session, url: str, context):
    try:
        time.sleep(REQUEST_DELAY)
        # REMOVED PROXY SETUP
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "").split(';')[0]
    except requests.exceptions.RequestException as e:
        logger.warning(f"Static fetch failed for {url}: {e}")
        return None, ""

def get_local_path(url: str, base_dir: Path, subdir: str) -> Path:
    parsed = urlparse(url)
    path = parsed.netloc + parsed.path
    if path.endswith('/'): 
        path += 'index.html'
    if url.rstrip('/') == f"{parsed.scheme}://{parsed.netloc}": 
        path = parsed.netloc + '/index.html'
    
    # FIX: Preserve directory structure by only replacing illegal characters
    path = re.sub(r'[<>:"|?*]', '_', path) # Keep slashes
    return base_dir / subdir / Path(path)

def save_content(path: Path, data: bytes):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock: path.write_bytes(data)
    except OSError as e: logger.error(f"Failed to save to {path}: {e}")

def find_css_assets(css_text: str, css_url: str, base_url: str, root_dir: Path) -> set:
    found_assets = set()
    for match in CSS_URL_RE.finditer(css_text):
        asset_url = urljoin(css_url, match.group(1))
        if asset_url.startswith(base_url):
            found_assets.add((asset_url, get_local_path(asset_url, root_dir, 'assets')))
    return found_assets

def scrape_page(url: str, depth: int, base_url: str, root_dir: Path, session: requests.Session, context, to_crawl: set, crawled_pages: set, assets_to_download: set):
    if url in crawled_pages or depth > DEFAULT_MAX_DEPTH: return
    crawled_pages.add(url)
    logger.info(f"Scraping page: {url} at depth {depth}")

    content, content_type = fetch_static(session, url, context)
    
    if context and (not content or (content and 'text/html' in content_type and len(BeautifulSoup(content, "html.parser").body.get_text(strip=True)) < DYNAMIC_SCRAPE_THRESHOLD)):
        logger.info(f"Static content for {url} is sparse or failed. Attempting dynamic scrape with Playwright.")
        dynamic_content = fetch_with_playwright(url, context)
        if dynamic_content:
            content = dynamic_content.encode('utf-8')
            content_type = 'text/html'

    if not content or 'text/html' not in content_type:
        logger.warning(f"No valid HTML content found for {url}")
        return

    soup = BeautifulSoup(content, "html.parser")
    page_local_path = get_local_path(url, root_dir, 'html')
    
    asset_map = {"link": "css", "script": "js", "img": "images"}
    
    for tag in soup.find_all(['a', 'link', 'script', 'img']):
        attr = "href" if tag.name in ('a', 'link') else "src"
        if not tag.has_attr(attr) or not tag[attr]: continue
            
        asset_url = urljoin(url, tag[attr].split('#')[0])
        if not asset_url.startswith(base_url): continue

        if tag.name == 'a':
            if depth < DEFAULT_MAX_DEPTH and asset_url.rstrip('/') != url.rstrip('/'):
                to_crawl.add(asset_url)
            asset_local_path = get_local_path(asset_url, root_dir, 'html')
        else:
            asset_subdir = asset_map.get(tag.name, 'assets')
            asset_local_path = get_local_path(asset_url, root_dir, asset_subdir)
            assets_to_download.add((asset_url, asset_local_path))
            
            if asset_subdir == 'css':
                css_content, _ = fetch_static(session, asset_url, None)
                if css_content:
                    assets_to_download.update(find_css_assets(css_content.decode('utf-8', 'ignore'), asset_url, base_url, root_dir))
        
        try:
            relative_path = os.path.relpath(asset_local_path, start=page_local_path.parent)
            tag[attr] = Path(relative_path).as_posix()
        except ValueError:
            tag[attr] = asset_local_path.as_posix()

    save_content(page_local_path, soup.encode("utf-8"))

def create_zip_from_directory(source_dir: Path, zip_path: Path) -> str:
    logger.info(f"Creating archive: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(source_dir.rglob("*")):
            if p.is_file(): zf.write(p, arcname=p.relative_to(source_dir))
    return str(zip_path)

def sanitize_name(name: str) -> str:
    name = re.sub(r'[^a-zA-Z0-9_]', ' ', name)
    return "".join(word.capitalize() for word in name.split())

def decompose_html(soup: BeautifulSoup) -> dict:
    decomposed = {"shared_components": {}, "page_specific_content": None}
    for tag_name in ['header', 'footer', 'nav']:
        element = soup.find(tag_name)
        if element:
            comp_name = tag_name.capitalize()
            decomposed["shared_components"][comp_name] = str(element)
            element.decompose()
    if soup.body:
        decomposed["page_specific_content"] = str(soup.body)
    return decomposed

# --- MAIN WORKFLOW FUNCTIONS ---


def run_scrape_workflow(base_url: str, depth: int = DEFAULT_MAX_DEPTH, workers: int = DEFAULT_MAX_WORKERS, output: str = None, user_agent: str = DEFAULT_USER_AGENT):
    base_url = base_url.rstrip("/")
    scrape_id = f"{urlparse(base_url).netloc}_{int(time.time())}"
    
    output_dir = Path(output or OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True)
    scrape_dir = output_dir / scrape_id
    if scrape_dir.exists(): shutil.rmtree(scrape_dir)
    scrape_dir.mkdir(parents=True)

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    
    browser, context = setup_playwright_browser()

    to_crawl, crawled_pages, assets_to_download = {base_url}, set(), set()
    
    for current_depth in range(depth + 1):
        urls_to_process = list(to_crawl - crawled_pages)
        if not urls_to_process: break
        logger.info(f"--- Crawling depth {current_depth}: {len(urls_to_process)} pages ---")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(scrape_page, url, current_depth, base_url, scrape_dir, session, context, to_crawl, crawled_pages, assets_to_download) for url in urls_to_process]
            for future in as_completed(futures): future.result()

    if browser:
        context.close()
        browser.close()

    logger.info(f"--- Downloading {len(assets_to_download)} assets ---")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_static, session, url, None): path for url, path in assets_to_download}
        for future in as_completed(future_map):
            content, _ = future.result()
            if content: save_content(future_map[future], content)

    # --- FIX: Generate proper relative paths ---
    file_list = [
        {
            "name": p.name,
            "path": str(p.relative_to(output_dir))
        } 
        for p in sorted(scrape_dir.rglob("*")) if p.is_file()
    ]

    # --- FIX: Save the zip file to the project root where the view expects it ---
    zip_filename = f"{urlparse(base_url).netloc}_scraped.zip"
    zip_path = output_dir.parent / zip_filename
    create_zip_from_directory(scrape_dir, zip_path)
    
    logger.info(f"Scraping complete. Zipped content at: {zip_path}")
    # --- FIX: Return the file_list and zip_path as expected by the view ---
    return file_list, str(zip_path)

def run_tailwind_conversion(source_dir_str: str):
    source_dir = Path(source_dir_str)
    project_name = f"{source_dir.name}-tailwind"
    target_dir = source_dir.parent / project_name
    if target_dir.exists(): shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    logger.info(f"--- Starting Tailwind Conversion for {source_dir_str} ---")
    for file_path in target_dir.rglob("*.css"):
        tailwind_result = convert_css_to_tailwind(file_path.read_text(encoding='utf-8', errors='ignore'))
        if tailwind_result and 'tailwind_classes' in tailwind_result:
            save_content(file_path.with_suffix('.tailwind.txt'), tailwind_result['tailwind_classes'].encode('utf-8'))
    
    # --- FIX: Save the zip file to the project root where the view expects it ---
    zip_filename = f"{project_name}.zip"
    zip_path = target_dir.parent / zip_filename
    create_zip_from_directory(target_dir, zip_path)
    
    return str(zip_path)

def run_react_conversion_workflow(source_dir_str: str):
    source_dir = Path(source_dir_str)
    project_name = f"{source_dir.name}-react-pro"
    target_dir = source_dir.parent / project_name
    if target_dir.exists(): shutil.rmtree(target_dir)
    
    logger.info(f"--- Generating Professional React Project: {project_name} ---")
    log_memory_usage()
    
    # Create directory structure
    src_dir, pages_dir, components_dir, layouts_dir, public_dir = target_dir / "src", target_dir / "src/pages", target_dir / "src/components", target_dir / "src/layouts", target_dir / "public"
    for d in [pages_dir, components_dir, layouts_dir, public_dir]: d.mkdir(parents=True, exist_ok=True)

    # Create package.json
    package_json = {
        "name": project_name.lower().replace("_", "-"),
        "version": "0.1.0",
        "private": True,
        "dependencies": {
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "react-router-dom": "^6.23.1",
            "react-scripts": "5.0.1"
        },
        "scripts": {
            "start": "react-scripts start",
            "build": "react-scripts build"
        },
        "devDependencies": {
            "tailwindcss": "^3.4.3"
        },
        "eslintConfig": {
            "extends": ["react-app", "react-app/jest"]
        },
        "browserslist": {
            "production": [">0.2%", "not dead", "not op_mini all"],
            "development": ["last 1 chrome version", "last 1 firefox version", "last 1 safari version"]
        }
    }
    save_content(target_dir / "package.json", json.dumps(package_json, indent=2).encode('utf-8'))
    
    # Create Tailwind config
    tailwind_config = "/** @type {import('tailwindcss').Config} */\nmodule.exports = {\n  content: [\"./src/**/*.{js,jsx,ts,tsx}\"],\n  theme: {\n    extend: {},\n  },\n  plugins: [],\n}"
    save_content(target_dir / "tailwind.config.js", tailwind_config.encode('utf-8'))
    
    # Create public files
    public_index_html = f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{project_name}</title></head><body><div id="root"></div></body></html>'
    save_content(public_dir / 'index.html', public_index_html.encode('utf-8'))
    
    # Create CSS files
    index_css_content = "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n"
    save_content(src_dir / 'index.css', index_css_content.encode('utf-8'))

    # Process HTML files in chunks
    page_info = []
    shared_components = set()
    html_source_dir = source_dir / "html"
    html_files = sorted(list(html_source_dir.rglob("*.html")))
    chunk_size = 3  # Process 3 files at a time to manage memory
    
    logger.info(f"Processing {len(html_files)} HTML files in chunks of {chunk_size}")
    
    for i in range(0, len(html_files), chunk_size):
        chunk_files = html_files[i:i+chunk_size]
        logger.info(f"Processing chunk {i//chunk_size + 1}: {[f.name for f in chunk_files]}")
        log_memory_usage()
        
        for html_file_path in chunk_files:
            try:
                html_content = html_file_path.read_text(encoding='utf-8', errors='ignore')
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # Extract CSS content
                css_contents = []
                for link_tag in soup.find_all('link', rel='stylesheet', href=True):
                    css_file_path = (html_file_path.parent / Path(link_tag['href'])).resolve()
                    if css_file_path.exists():
                        css_contents.append(css_file_path.read_text(encoding='utf-8', errors='ignore'))
                
                # Decompose HTML
                decomposed_parts = decompose_html(soup)
                if not decomposed_parts: continue

                page_name = sanitize_name(Path(html_file_path.stem).name) or "Home"
                page_name += "Page"
                
                # Process shared components
                for comp_name, comp_html in decomposed_parts.get("shared_components", {}).items():
                    if comp_name not in shared_components:
                        logger.info(f"Converting shared component: {comp_name}")
                        react_result = convert_html_snippet_to_component(comp_html, "\n".join(css_contents), comp_name)
                        if react_result and 'react_component' in react_result:
                            component_body = react_result['react_component']
                            
                            # Extract JSX return statement
                            match = re.search(r"return\s*\(([\s\S]*)\);?\s*\}?", component_body, re.DOTALL)
                            if match: 
                                component_body = match.group(1).strip()
                            
                            # Add necessary imports
                            imports = "import React from 'react';\n"
                            if "<Link" in component_body: 
                                imports += "import { Link } from 'react-router-dom';\n"
                            
                            # Format component code
                            component_code = f"{imports}\nconst {comp_name} = () => {{\n  return (\n    {component_body}\n  );\n}};\n\nexport default {comp_name};\n"
                            save_content(components_dir / f"{comp_name}.jsx", component_code.encode('utf-8'))
                            shared_components.add(comp_name)
                        else:
                            logger.warning(f"Failed to convert component: {comp_name}")

                # Process page content
                page_html = decomposed_parts.get("page_specific_content")
                if page_html:
                    logger.info(f"Converting page: {page_name}")
                    react_result = convert_html_snippet_to_component(page_html, "\n".join(css_contents), page_name)
                    if react_result and 'react_component' in react_result:
                        # Add necessary imports
                        imports = "import React"
                        if "useState" in react_result['react_component']: 
                            imports += ", { useState }"
                        imports += " from 'react';\n"
                        
                        if "<Link" in react_result['react_component']: 
                            imports += "import { Link } from 'react-router-dom';\n"
                        
                        component_body = react_result['react_component']
                        
                        # Extract JSX return statement
                        match = re.search(r"return\s*\(([\s\S]*)\);?\s*\}?", component_body, re.DOTALL)
                        if match: 
                            component_body = match.group(1).strip()

                        # Format page component
                        component_code = f"{imports}\nconst {page_name} = () => {{\n  return (\n    <>\n      {component_body}\n    </>\n  );\n}};\n\nexport default {page_name};\n"
                        save_content(pages_dir / f"{page_name}.jsx", component_code.encode('utf-8'))
                        
                        # Create route path
                        route_path = f"/{Path(html_file_path.stem).name}" 
                        if Path(html_file_path.stem).name.lower() != 'index' else '/'
                        
                        page_info.append({
                            "name": page_name, 
                            "path": route_path, 
                            "displayName": page_name.replace("Page", "")
                        })
                    else:
                        logger.warning(f"Failed to convert page: {page_name}")
            except Exception as e:
                logger.error(f"Could not process HTML file {html_file_path}: {e}")

    # Create main layout
    if shared_components:
        logger.info("Creating main layout with shared components")
        layout_imports = "\n".join([f"import {s} from '../components/{s}';" for s in shared_components])
        layout_render = "\n".join([f"      <{s} />" for s in shared_components])
        main_layout_content = f"import React from 'react';\nimport {{ Outlet }} from 'react-router-dom';\n{layout_imports}\n\nconst MainLayout = () => {{\n  return (\n    <>\n{layout_render}\n      <main><Outlet /></main>\n    </>\n  );\n}};\n\nexport default MainLayout;\n"
        save_content(layouts_dir / 'MainLayout.jsx', main_layout_content.encode('utf-8'))
    else:
        logger.warning("No shared components found for layout")

    # Create App.js
    if page_info:
        logger.info("Creating App.js with routes")
        app_imports = "import React from 'react';\nimport { BrowserRouter as Router, Routes, Route } from 'react-router-dom';\n"
        if shared_components:
            app_imports += "import MainLayout from './layouts/MainLayout';\n"
        app_imports += "\n".join([f"import {p['name']} from './pages/{p['name']}';" for p in page_info])
        
        app_routes = "\n".join([f"        <Route path=\"{p['path']}\" element={{<{p['name']} />}} />" for p in page_info])
        
        # Wrap routes in layout if available
        if shared_components:
            app_js_content = f"{app_imports}\nimport './index.css';\n\nfunction App() {{\n  return (\n    <Router>\n      <Routes>\n        <Route element={{<MainLayout />}}>\n{app_routes}\n        </Route>\n      </Routes>\n    </Router>\n  );\n}}\n\nexport default App;\n"
        else:
            app_js_content = f"{app_imports}\nimport './index.css';\n\nfunction App() {{\n  return (\n    <Router>\n      <Routes>\n{app_routes}\n      </Routes>\n    </Router>\n  );\n}}\n\nexport default App;\n"
        
        save_content(src_dir / 'App.js', app_js_content.encode('utf-8'))
    else:
        logger.error("No pages created for App.js")

    # Create index.js
    index_js_content = "import React from 'react';\nimport ReactDOM from 'react-dom/client';\nimport App from './App';\n\nconst root = ReactDOM.createRoot(document.getElementById('root'));\nroot.render(\n  <React.StrictMode>\n    <App />\n  </React.StrictMode>\n);"
    save_content(src_dir / 'index.js', index_js_content.encode('utf-8'))

    # Copy assets
    logger.info("Copying assets to public directory")
    if (source_dir / "images").exists(): 
        shutil.copytree(source_dir / "images", public_dir / "images")
    if (source_dir / "assets").exists(): 
        shutil.copytree(source_dir / "assets", public_dir / "assets")

    # Create zip archive
    logger.info("Creating final zip archive")
    zip_filename = f"{project_name}.zip"
    zip_path = target_dir.parent / zip_filename
    create_zip_from_directory(target_dir, zip_path)
    
    logger.info(f"React conversion complete: {zip_path}")
    log_memory_usage()
    
    return str(zip_path)