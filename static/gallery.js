// Transit Image Gallery JavaScript

let allImages = [];
let currentFilter = 'all';
let currentImagePath = '';
let currentImageMetadata = {};

// Gallery authentication helpers
function getGalleryAuthToken() {
    // Try to get from localStorage
    let token = localStorage.getItem('galleryAuthToken');
    
    // If not found, prompt user
    if (!token) {
        token = prompt('Enter gallery authentication token (from .env GALLERY_AUTH_TOKEN):');
        if (token) {
            // Store for this session
            localStorage.setItem('galleryAuthToken', token);
        }
    }
    
    return token;
}

function clearGalleryAuthToken() {
    localStorage.removeItem('galleryAuthToken');
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadGallery();
    setupUploadForm();
    setupFilters();
    setupLightbox();
    setupEditModal();
});

// Load all gallery images
async function loadGallery() {
    try {
        const response = await fetch('/gallery/list');
        allImages = await response.json();
        console.log('Gallery loaded:', allImages.length, 'images', allImages);
        displayGallery();
    } catch (error) {
        console.error('Error loading gallery:', error);
        document.getElementById('galleryGrid').innerHTML = '<div class="no-images">Error loading gallery</div>';
    }
}

// Display gallery with current filter
function displayGallery() {
    const grid = document.getElementById('galleryGrid');

    // Filter images
    let filteredImages = allImages;
    if (currentFilter !== 'all') {
        filteredImages = allImages.filter(img => img.metadata.target === currentFilter);
    }

    // Display images
    if (filteredImages.length === 0) {
        grid.innerHTML = '<div class="no-images">No images yet. Upload your first transit image!</div>';
        return;
    }

    grid.innerHTML = filteredImages.map(img => {
        const meta = img.metadata;
        const target = meta.target === 'moon' ? 'Moon' : meta.target === 'sun' ? 'Sun' : 'Other';
        const flightInfo = meta.flight_id ? `${meta.flight_id}${meta.aircraft_type ? ' (' + meta.aircraft_type + ')' : ''}` : 'Unknown Flight';
        const date = meta.timestamp ? new Date(meta.timestamp).toLocaleDateString() : 'Unknown Date';
        const caption = meta.caption ? `<div class="gallery-item-caption">${escapeHtml(meta.caption)}</div>` : '';

        return `
            <div class="gallery-item" onclick="openLightbox('${img.path}', ${JSON.stringify(meta).replace(/"/g, '&quot;')})">
                <img src="/${img.path}" alt="${flightInfo}" loading="lazy">
                <div class="gallery-item-info">
                    <div class="gallery-item-title">${target} ${escapeHtml(flightInfo)}</div>
                    <div class="gallery-item-meta">
                        ${date}
                        ${meta.equipment ? '<br>' + escapeHtml(meta.equipment) : ''}
                    </div>
                    ${caption}
                </div>
            </div>
        `;
    }).join('');
}

// Setup upload form
function setupUploadForm() {
    const form = document.getElementById('uploadForm');
    const status = document.getElementById('uploadStatus');

    form.addEventListener('submit', async function(e) {
        e.preventDefault();

        const formData = new FormData(form);

        // Show uploading status
        status.className = '';
        status.textContent = '⏳ Uploading...';
        status.style.display = 'block';

        try {
            // Get auth token from localStorage or prompt
            const authToken = getGalleryAuthToken();
            if (!authToken) {
                alert('Gallery authentication required. Please set GALLERY_AUTH_TOKEN.');
                return;
            }

            const response = await fetch('/gallery/upload', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${authToken}`
                },
                body: formData
            });

            const result = await response.json();

            if (response.ok) {
                status.className = 'success';
                status.textContent = '✅ Image uploaded successfully! Refreshing gallery...';
                form.reset();
                // Reload gallery after short delay to ensure file is written
                setTimeout(() => {
                    loadGallery();
                }, 1000);
                setTimeout(() => {
                    status.style.display = 'none';
                }, 3000);
            } else {
                status.className = 'error';
                status.textContent = '❌ Error: ' + (result.error || 'Upload failed');
            }
        } catch (error) {
            status.className = 'error';
            status.textContent = '❌ Error: ' + error.message;
        }
    });
}

// Setup filter radio buttons
function setupFilters() {
    const radios = document.querySelectorAll('input[name="targetFilter"]');
    radios.forEach(radio => {
        radio.addEventListener('change', function() {
            currentFilter = this.value;
            displayGallery();
        });
    });
}

// Setup lightbox
function setupLightbox() {
    const lightbox = document.getElementById('lightbox');
    const closeBtn = document.querySelector('.lightbox-close');
    const deleteBtn = document.getElementById('deleteImageBtn');
    const editBtn = document.getElementById('editImageBtn');

    closeBtn.addEventListener('click', closeLightbox);

    lightbox.addEventListener('click', function(e) {
        if (e.target === lightbox) {
            closeLightbox();
        }
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeLightbox();
            closeEditModal();
        }
    });

    // Delete button
    deleteBtn.addEventListener('click', async function() {
        if (!currentImagePath) return;

        if (confirm('Are you sure you want to delete this image? This cannot be undone.')) {
            try {
                // Get auth token
                const authToken = getGalleryAuthToken();
                if (!authToken) {
                    alert('Gallery authentication required.');
                    return;
                }

                const response = await fetch(`/gallery/delete/${currentImagePath}`, {
                    method: 'DELETE',
                    headers: {
                        'Authorization': `Bearer ${authToken}`
                    }
                });

                if (response.ok) {
                    closeLightbox();
                    loadGallery();
                    alert('Image deleted successfully');
                } else {
                    const error = await response.json();
                    alert('Error deleting image: ' + (error.error || 'Unknown error'));
                }
            } catch (error) {
                alert('Error deleting image: ' + error.message);
            }
        }
    });

    // Edit button
    editBtn.addEventListener('click', function() {
        openEditModal();
    });
}

// Open lightbox with image
function openLightbox(imagePath, metadata) {
    const lightbox = document.getElementById('lightbox');
    const lightboxImage = document.getElementById('lightboxImage');
    const lightboxInfo = document.getElementById('lightboxInfo');

    // Store current image info for edit/delete
    currentImagePath = imagePath;
    currentImageMetadata = metadata;

    lightboxImage.src = '/' + imagePath;

    const target = metadata.target === 'moon' ? 'Moon' : metadata.target === 'sun' ? 'Sun' : 'Unknown';
    const flightInfo = metadata.flight_id ? `${metadata.flight_id}${metadata.aircraft_type ? ' (' + metadata.aircraft_type + ')' : ''}` : 'Unknown Flight';
    const date = metadata.timestamp ? new Date(metadata.timestamp).toLocaleString() : 'Unknown Date';

    let infoHTML = `
        <h3>${escapeHtml(flightInfo)}</h3>
        <p><strong>Target:</strong> ${target}</p>
        <p><strong>Date:</strong> ${date}</p>
    `;

    if (metadata.equipment) {
        infoHTML += `<p><strong>Equipment:</strong> ${escapeHtml(metadata.equipment)}</p>`;
    }

    if (metadata.observer_lat && metadata.observer_lon) {
        infoHTML += `<p><strong>Location:</strong> ${metadata.observer_lat}, ${metadata.observer_lon}</p>`;
    }

    if (metadata.caption) {
        infoHTML += `<p><strong>Caption:</strong> ${escapeHtml(metadata.caption)}</p>`;
    }

    lightboxInfo.innerHTML = infoHTML;
    lightbox.style.display = 'flex';
}

// Close lightbox
function closeLightbox() {
    document.getElementById('lightbox').style.display = 'none';
}

// Setup edit modal
function setupEditModal() {
    const modal = document.getElementById('editModal');
    const closeBtn = document.querySelector('.modal-close');
    const form = document.getElementById('editForm');

    closeBtn.addEventListener('click', closeEditModal);

    modal.addEventListener('click', function(e) {
        if (e.target === modal) {
            closeEditModal();
        }
    });

    form.addEventListener('submit', async function(e) {
        e.preventDefault();

        const formData = new FormData(form);
        const filepath = formData.get('filepath');

        try {
            // Get auth token
            const authToken = getGalleryAuthToken();
            if (!authToken) {
                alert('Gallery authentication required.');
                return;
            }

            const response = await fetch(`/gallery/update/${filepath}`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${authToken}`
                },
                body: formData
            });

            const result = await response.json();

            if (response.ok) {
                closeEditModal();
                closeLightbox();
                loadGallery();
                alert('Metadata updated successfully');
            } else {
                alert('Error updating metadata: ' + (result.error || 'Unknown error'));
            }
        } catch (error) {
            alert('Error updating metadata: ' + error.message);
        }
    });
}

function openEditModal() {
    if (!currentImagePath || !currentImageMetadata) return;

    const modal = document.getElementById('editModal');

    // Populate form with current metadata
    document.getElementById('edit_filepath').value = currentImagePath;
    document.getElementById('edit_flight_id').value = currentImageMetadata.flight_id || '';
    document.getElementById('edit_aircraft_type').value = currentImageMetadata.aircraft_type || '';
    document.getElementById('edit_target').value = currentImageMetadata.target || '';
    document.getElementById('edit_equipment').value = currentImageMetadata.equipment || '';
    document.getElementById('edit_caption').value = currentImageMetadata.caption || '';
    document.getElementById('edit_observer_lat').value = currentImageMetadata.observer_lat || '';
    document.getElementById('edit_observer_lon').value = currentImageMetadata.observer_lon || '';

    modal.style.display = 'flex';
}

function closeEditModal() {
    document.getElementById('editModal').style.display = 'none';
}

// Escape HTML to prevent XSS
function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}
