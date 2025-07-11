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

# Import the correct functions from your provided scraper file
from .scraper import run_scrape_workflow, run_react_conversion_workflow, run_tailwind_conversion

logger = logging.getLogger(__name__)

def index(request: HttpRequest) -> HttpResponse:
    """
    Handles the main page logic. A POST request starts a new scrape,
    while a GET request clears the session for a fresh start.
    """
    if request.method == "GET" and 'scrape_dir' in request.session:
        del request.session['scrape_dir']
        
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        if not url.startswith(('http://', 'https://')):
            messages.error(request, "Invalid URL. Please ensure it starts with http:// or https://")
            return render(request, "scraperappv2/index.html")
        
        try:
            # FIX: Get correct return values from scraper
            file_list, zip_path = run_scrape_workflow(url)
            
            # Extract directory from first file path
            if file_list:
                # The scraper now correctly returns the list of files and the directory.
                # We can get the directory from the first file's path.
                # The scraper returns a path relative to the 'mirror_upgraded' folder.
                # We need the full path to the unique scrape folder.
                first_file_rel_path = Path(file_list[0]['path'])
                scrape_dir_name = first_file_rel_path.parts[0]
                scrape_dir_full_path = Path(settings.BASE_DIR) / "mirror_upgraded" / scrape_dir_name
                
                # Store the unique output directory in the session
                request.session['scrape_dir'] = str(scrape_dir_full_path)
            else:
                request.session['scrape_dir'] = None
            
            # Add URLs for each file
            for file_info in file_list:
                file_info['download_url'] = reverse('scraperappv2:scraper_download_file', kwargs={'filepath': file_info['path']})
                if file_info['path'].endswith(('.html', '.htm')):
                    file_info['preview_url'] = reverse('scraperappv2:scraper_serve_file', kwargs={'filepath': file_info['path']})

            context = {
                "files": file_list,
                "zip_download_url": reverse('scraperappv2:scraper_download_zip', kwargs={'filename': os.path.basename(zip_path)}),
                "scrape_session_active": bool(file_list) # Flag to show conversion buttons
            }
            
            if file_list:
                messages.success(request, f"Successfully scraped {len(file_list)} files. You can now perform AI conversions.")
            else:
                messages.warning(request, "Scrape completed but no files were found.")
                
            return render(request, "scraperappv2/index.html", context)

        except Exception as e:
            logger.exception("An error occurred during scraping.")
            messages.error(request, f"An unexpected error occurred: {str(e)}")
            return render(request, "scraperappv2/index.html")

    return render(request, "scraperappv2/index.html")


@require_POST
@csrf_exempt
def trigger_conversion(request: HttpRequest) -> JsonResponse:
    """
    Handles on-demand AI conversion requests sent from the front-end.
    """
    try:
        data = json.loads(request.body)
        conversion_type = data.get('conversion_type')
        source_dir = request.session.get('scrape_dir')

        if not source_dir:
            return JsonResponse({'error': 'No active scrape session found. Please scrape a site first.'}, status=400)

        if conversion_type == 'react':
            new_zip_path_str = run_react_conversion_workflow(source_dir)
        elif conversion_type == 'tailwind':
             new_zip_path_str = run_tailwind_conversion(source_dir)
        else:
            return JsonResponse({'error': 'Invalid conversion type.'}, status=400)
        
        new_zip_path = Path(new_zip_path_str)
        download_url = reverse('scraperappv2:scraper_download_zip', kwargs={'filename': new_zip_path.name})

        return JsonResponse({
            'success': True,
            'download_url': download_url,
            'filename': new_zip_path.name
        })

    except Exception as e:
        logger.exception("An error occurred during AI conversion.")
        return JsonResponse({'error': str(e)}, status=500)


# --- File Serving Views ---

def serve_mirrored_file(request: HttpRequest, filepath: str) -> HttpResponse:
    # Use the base 'mirror_upgraded' directory for security checks
    base_path = (Path(settings.BASE_DIR) / "mirror_upgraded").resolve()
    file_path = (base_path / filepath).resolve()

    # Security check
    if not str(file_path).startswith(str(base_path)):
        return HttpResponseForbidden("Access Denied.")
    if not file_path.exists() or not file_path.is_file():
        raise Http404(f"File not found: {filepath}")

    content_type, _ = mimetypes.guess_type(file_path)
    return FileResponse(open(file_path, "rb"), content_type=content_type)


def download_file(request: HttpRequest, filepath: str) -> HttpResponse:
    base_path = (Path(settings.BASE_DIR) / "mirror_upgraded").resolve()
    file_path = (base_path / filepath).resolve()

    # Security check
    if not str(file_path).startswith(str(base_path)):
        return HttpResponseForbidden("Access Denied.")
    if not file_path.exists() or not file_path.is_file():
        raise Http404(f"File not found: {filepath}")

    return FileResponse(open(file_path, "rb"), as_attachment=True, filename=file_path.name)


def download_zip(request: HttpRequest, filename: str) -> HttpResponse:
    # Validate filename format
    if not filename.endswith('.zip') or '..' in filename or '/' in filename:
        return HttpResponseForbidden("Invalid filename")
    
    # Look for the zip file in the project's root directory
    zip_path = (Path(settings.BASE_DIR) / filename).resolve()
    
    # Security check to ensure the file is in the project root
    if not str(zip_path.parent) == str(Path(settings.BASE_DIR).resolve()):
         return HttpResponseForbidden("Access Denied.")

    if not zip_path.exists() or not zip_path.is_file():
        raise Http404(f"Archive not found: {filename}")
        
    return FileResponse(open(zip_path, "rb"), as_attachment=True, filename=filename)