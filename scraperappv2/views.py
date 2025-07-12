import logging
import json
from pathlib import Path
import mimetypes
import os

from django.shortcuts import render
from django.contrib import messages
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from asgiref.sync import sync_to_async  # for offloading sync tasks to thread pool

# Import scraping functions
from .scraper import run_scrape_workflow, run_react_conversion_workflow, run_tailwind_conversion

logger = logging.getLogger(__name__)

# Synchronous helpers

def clear_session_sync(session):
    session.pop('scrape_dir', None)

def add_message_sync(request, level, msg):
    getattr(messages, level)(request, msg)

def render_sync(request, template, context=None):
    return render(request, template, context or {})

# Async wrappers (offload to thread pool)
async_clear_session = sync_to_async(clear_session_sync, thread_sensitive=False)
async_add_message = sync_to_async(add_message_sync, thread_sensitive=False)
async_render = sync_to_async(render_sync, thread_sensitive=False)

async def index(request: HttpRequest) -> HttpResponse:
    # On GET, clear any previous session data
    if request.method == "GET" and 'scrape_dir' in request.session:
        await async_clear_session(request.session)

    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        if not url.startswith(('http://', 'https://')):
            await async_add_message(request, 'error', "Invalid URL. Please ensure it starts with http:// or https://")
            return await async_render(request, "scraperappv2/index.html")

        try:
            # Offload the blocking scrape workflow to a thread
            file_list, zip_path = await sync_to_async(run_scrape_workflow, thread_sensitive=False)(url)

            # Determine scrape directory
            if file_list:
                first_path = Path(file_list[0]['path'])
                scrape_dir_name = first_path.parts[0]
                full_dir = Path(settings.BASE_DIR) / "mirror_upgraded" / scrape_dir_name
                request.session['scrape_dir'] = str(full_dir)
                await sync_to_async(request.session.save, thread_sensitive=False)()
            else:
                request.session['scrape_dir'] = None

            # Build URLs
            for info in file_list:
                info['download_url'] = reverse('scraperappv2:scraper_download_file', kwargs={'filepath': info['path']})
                if info['path'].endswith(('.html', '.htm')):
                    info['preview_url'] = reverse('scraperappv2:scraper_serve_file', kwargs={'filepath': info['path']})

            context = {
                'files': file_list,
                'zip_download_url': reverse('scraperappv2:scraper_download_zip', kwargs={'filename': os.path.basename(zip_path)}),
                'scrape_session_active': bool(file_list)
            }

            level = 'success' if file_list else 'warning'
            msg = f"Successfully scraped {len(file_list)} files." if file_list else "Scrape completed but no files found."
            await async_add_message(request, level, msg)

            return await async_render(request, "scraperappv2/index.html", context)

        except Exception as e:
            logger.exception("Scraping error")
            await async_add_message(request, 'error', f"An unexpected error occurred: {e}")
            return await async_render(request, "scraperappv2/index.html")

    # Default: render page
    return await async_render(request, "scraperappv2/index.html")


@require_POST
@csrf_exempt
async def trigger_conversion(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body)
        conversion = data.get('conversion_type')
        source = request.session.get('scrape_dir')
        if not source:
            return JsonResponse({'error': 'No active scrape session found.'}, status=400)

        if conversion == 'react':
            zip_str = await sync_to_async(run_react_conversion_workflow, thread_sensitive=False)(source)
        elif conversion == 'tailwind':
            zip_str = await sync_to_async(run_tailwind_conversion, thread_sensitive=False)(source)
        else:
            return JsonResponse({'error': 'Invalid conversion type.'}, status=400)

        name = Path(zip_str).name
        download_url = reverse('scraperappv2:scraper_download_zip', kwargs={'filename': name})
        return JsonResponse({'success': True, 'download_url': download_url, 'filename': name})

    except Exception as e:
        logger.exception("Conversion error")
        return JsonResponse({'error': str(e)}, status=500)


# File serving remains synchronous

def serve_mirrored_file(request: HttpRequest, filepath: str) -> HttpResponse:
    base = (Path(settings.BASE_DIR) / "mirror_upgraded").resolve()
    target = (base / filepath).resolve()
    if not target.exists() or not target.is_file() or not str(target).startswith(str(base)):
        raise Http404
    ct, _ = mimetypes.guess_type(target)
    return FileResponse(open(target, 'rb'), content_type=ct)


def download_file(request: HttpRequest, filepath: str) -> HttpResponse:
    base = (Path(settings.BASE_DIR) / "mirror_upgraded").resolve()
    target = (base / filepath).resolve()
    if not target.exists() or not target.is_file() or not str(target).startswith(str(base)):
        raise Http404
    return FileResponse(open(target, 'rb'), as_attachment=True, filename=target.name)


def download_zip(request: HttpRequest, filename: str) -> HttpResponse:
    if not filename.endswith('.zip') or '..' in filename or '/' in filename:
        return HttpResponseForbidden()
    root = Path(settings.BASE_DIR).resolve()
    zip_path = (root / filename).resolve()
    if not zip_path.exists() or not str(zip_path.parent) == str(root):
        raise Http404
    return FileResponse(open(zip_path, 'rb'), as_attachment=True, filename=filename)
