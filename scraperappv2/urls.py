from django.urls import path
from . import views


app_name = 'scraperappv2'

urlpatterns = [
  
    path('', views.index, name='scraper_index'),
    
    # AI conversion requests
    path('convert/', views.trigger_conversion, name='scraper_trigger_conversion'),

    #  downloading various zip archives (original, tailwind, react)
    path('download_zip/<str:filename>/', views.download_zip, name='scraper_download_zip'),

    # downloading an individual file from the latest scrape session
    path('download/<path:filepath>/', views.download_file, name='scraper_download_file'),

    #  previewing scraped files from the latest scrape session
    path('mirror/<path:filepath>/', views.serve_mirrored_file, name='scraper_serve_file'),
]
