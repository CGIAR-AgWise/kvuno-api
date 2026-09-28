/* kvuno Data Explorer — filters, map, table, export */
(function () {
    'use strict';

    const state = {
        page: 1,
        perPage: 50,
        sortCol: null,
        sortDir: null,
    };

    let map = null;
    let markers = null;
    let heat = null;
    let heatVisible = false;
    let clusterLayer = null;
    let clusterVisible = false;

    // ── DOM refs ────────────────────────────────────────────────

    const $ = function (id) {
        return document.getElementById(id);
    };

    const filters = {
        country: $('f-country'),
        province: $('f-province'),
        variety: $('f-variety'),
        season: $('f-season'),
        date: $('f-date'),
        option: $('f-option'),
        radius: $('f-radius'),
        lon: $('f-lon'),
        lat: $('f-lat'),
    };

    const dataRows = $('data-rows');
    const pageInfo = $('page-info');
    const resultCount = $('result-count');
    const paginationSlot = $('pagination-slot');
    const perPageSelect = $('per-page');
    const clearBtn = $('clear-filters');
    const exportCsv = $('export-csv');
    const exportJson = $('export-json');

    // ── URL <-> state ───────────────────────────────────────────

    function paramsFromUrl() {
        const p = new URLSearchParams(location.search);
        filters.country.value = p.get('country') || '';
        filters.province.value = p.get('province') || '';
        filters.variety.value = p.get('variety') || '';
        filters.season.value = p.get('season_type') || '';
        filters.date.value = p.get('opt_date') || '';
        filters.option.value = p.get('planting_option') || '';
        filters.radius.value = p.get('radius') || '';
        filters.lon.value = p.get('lon') || '';
        filters.lat.value = p.get('lat') || '';
        state.page = parseInt(p.get('page')) || 1;
        state.perPage = parseInt(p.get('per_page')) || 200;
        state.sortCol = p.get('sort_col') || null;
        state.sortDir = p.get('sort_dir') || null;
    }

    function urlFromState() {
        const p = new URLSearchParams();
        forEachFilter(function (key, el) {
            if (el.value) p.set(key, el.value);
        });
        if (state.page > 1) p.set('page', state.page);
        if (state.perPage !== 200) p.set('per_page', state.perPage);
        if (state.sortCol) p.set('sort_col', state.sortCol);
        if (state.sortDir) p.set('sort_dir', state.sortDir);
        const q = p.toString();
        const url = location.pathname + (q ? '?' + q : '');
        history.replaceState(null, '', url);
    }

    function forEachFilter(fn) {
        const map = {
            country: 'country', province: 'province', variety: 'variety',
            season: 'season_type', date: 'opt_date', option: 'planting_option',
            radius: 'radius', lon: 'lon', lat: 'lat',
        };
        Object.keys(map).forEach(function (key) {
            fn(map[key], filters[key]);
        });
    }

    // ── Data fetching ───────────────────────────────────────────

    function buildQuery() {
        const p = new URLSearchParams();
        p.set('page', state.page);
        p.set('per_page', state.perPage);
        forEachFilter(function (key, el) {
            if (el.value) p.set(key, el.value);
        });
        if (filters.lon.value && filters.lat.value) {
            p.set('coordinates', filters.lon.value + ',' + filters.lat.value);
        }
        if (state.sortCol) p.set('sort_col', state.sortCol);
        if (state.sortDir) p.set('sort_dir', state.sortDir);
        return p.toString();
    }

    function fetchData() {
        perPageSelect.value = String(state.perPage);
        const q = buildQuery();
        urlFromState();
        document.querySelectorAll('.col-sort').forEach(function (th) {
            th.classList.remove('asc', 'desc');
            if (th.dataset.col === state.sortCol) th.classList.add(state.sortDir || 'asc');
        });
        return fetch('/api/v1/planting-data?' + q)
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                if (data.error) throw new Error(data.error);
                renderTable(data);
                renderMap(data);
                renderPagination(data);
                resultCount.textContent = data.total + ' record' + (data.total !== 1 ? 's' : '');
            })
            .catch(function (err) {
                dataRows.innerHTML = '<tr><td colspan="8" class="text-center text-danger small py-3">' + escHtml(err.message) + '</td></tr>';
                paginationSlot.innerHTML = '';
            });
    }

    // ── Table ───────────────────────────────────────────────────

    function renderTable(data) {
        const items = data.data || [];
        if (!items.length) {
            dataRows.innerHTML = '<tr><td colspan="8" class="text-center text-muted small py-3">No records match your filters.</td></tr>';
            return;
        }
        dataRows.innerHTML = items.map(function (r) {
            return '<tr>'
                + '<td>' + escHtml(r.country) + '</td>'
                + '<td>' + escHtml(r.province) + '</td>'
                + '<td>' + escHtml(r.variety) + '</td>'
                + '<td>' + escHtml(r.lon) + '</td>'
                + '<td>' + escHtml(r.lat) + '</td>'
                + '<td>' + escHtml(r.season_type) + '</td>'
                + '<td>' + escHtml(r.opt_date) + '</td>'
                + '<td>' + escHtml(r.planting_option) + '</td>'
                + '</tr>';
        }).join('');
    }

    // ── Pagination ──────────────────────────────────────────────

    function renderPagination(data) {
        const cur = data.current_page, pages = data.pages || 1;
        pageInfo.textContent = 'Page ' + cur + ' of ' + pages;
        const start = Math.max(1, cur - 2);
        const end = Math.min(pages, cur + 2);

        let h = '<div class="btn-group btn-group-sm me-2">';
        h += '<button class="btn btn-outline-secondary page-btn" data-page="' + (cur - 1) + '"' + (cur <= 1 ? ' disabled' : '') + '>\u2039</button>';
        if (start > 1) h += '<button class="btn btn-outline-secondary page-btn" data-page="1">1</button>' + (start > 2 ? '<button class="btn btn-outline-secondary page-btn" disabled>\u2026</button>' : '');
        for (let i = start; i <= end; i++) {
            h += '<button class="btn page-btn' + (i === cur ? ' btn-primary' : ' btn-outline-secondary') + '" data-page="' + i + '">' + i + '</button>';
        }
        if (end < pages) h += (end < pages - 1 ? '<button class="btn btn-outline-secondary page-btn" disabled>\u2026</button>' : '') + '<button class="btn btn-outline-secondary page-btn" data-page="' + pages + '">' + pages + '</button>';
        h += '<button class="btn btn-outline-secondary page-btn" data-page="' + (cur + 1) + '"' + (cur >= pages ? ' disabled' : '') + '>\u203a</button>';
        h += '</div>';
        paginationSlot.innerHTML = h;

        paginationSlot.querySelectorAll('.page-btn:not([disabled])').forEach(function (btn) {
            btn.addEventListener('click', function () {
                goToPage(parseInt(this.dataset.page));
            });
        });
    }

    function goToPage(page) {
        if (page < 1) return;
        state.page = page;
        fetchData().then(r => {
            console.debug(r);
        });
    }

    // ── Per-page selector ───────────────────────────────────────

    perPageSelect.addEventListener('change', function () {
        state.perPage = parseInt(this.value);
        state.page = 1;
        fetchData().then(r => {
            console.debug(r);
        });
    });

    // ── Map ──────────────────────────────────────────────────────

    function initMap() {
        if (map) return;
        map = L.map('explore-map').setView([-12, 28], 5);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap',
            maxZoom: 18,
        }).addTo(map);
        if (L.markerClusterGroup) {
            markers = L.markerClusterGroup({chunkedLoading: true});
        } else {
            markers = L.layerGroup();
        }
        map.addLayer(markers);
    }

    function renderMap(data) {
        initMap();
        if (heatVisible || clusterVisible) return;
        markers.clearLayers();
        const items = data.data || [];
        if (!items.length) return;

        const bounds = [];
        items.forEach(function (r) {
            const lat = parseFloat(r.lat);
            const lon = parseFloat(r.lon);
            if (isNaN(lat) || isNaN(lon)) return;
            const m = L.circleMarker([lat, lon], {
                radius: 5, fillColor: '#1976d2', color: '#fff',
                weight: 1, fillOpacity: 0.8,
            });
            const label = (r.country || '') + ' - ' + (r.variety || '')
                + '<br/>Date: ' + (r.opt_date || '')
                + '<br/>Option: ' + (r.planting_option || '');
            m.bindTooltip(label);
            markers.addLayer(m);
            bounds.push([lat, lon]);
        });

        if (bounds.length) {
            map.fitBounds(bounds, {padding: [20, 20], maxZoom: 12});
        }
    }

    // ── Export ───────────────────────────────────────────────────

    function exportFormat(fmt) {
        const q = buildQuery();
        const p = new URLSearchParams(q);
        p.set('format', fmt);
        const url = '/api/v1/planting-data/export?' + p.toString();
        const a = document.createElement('a');
        a.href = url;
        a.download = 'kvuno-export.' + fmt;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    // ── Event wiring ────────────────────────────────────────────

    let debounceTimer;

    function onFilterInput() {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
            state.page = 1;
            fetchData().then(r => {
                console.debug(r);
            });
        }, 350);
    }

    const selectFilters = {country: 1, province: 1, variety: 1, season: 1};
    Object.keys(filters).forEach(function (key) {
        filters[key].addEventListener(selectFilters[key] ? 'change' : 'input', onFilterInput);
    });

    clearBtn.addEventListener('click', function () {
        Object.keys(filters).forEach(function (key) {
            filters[key].value = '';
        });
        state.page = 1;
        fetchData().then(r => {
            console.debug(r);
        });
    });

    exportCsv.addEventListener('click', function () {
        exportFormat('csv');
    });
    exportJson.addEventListener('click', function () {
        exportFormat('json');
    });

    document.querySelectorAll('.col-sort').forEach(function (th) {
        th.addEventListener('click', function () {
            const col = this.dataset.col;
            if (state.sortCol === col) {
                state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
            } else {
                state.sortCol = col;
                state.sortDir = 'asc';
            }
            state.page = 1;
            fetchData().then(r => {
                console.debug(r);
            });
        });
    });

    // ── Heatmap toggle ──────────────────────────────────────────

    const heatmapBtn = $('toggle-heatmap');

    heatmapBtn.addEventListener('click', function () {
        heatVisible = !heatVisible;
        heatmapBtn.classList.toggle('active', heatVisible);
        heatmapBtn.innerHTML = heatVisible
            ? '<i class="bi bi-fire"></i> Points'
            : '<i class="bi bi-fire"></i> Heatmap';

        if (heatVisible) {
            if (markers) map.removeLayer(markers);
            showHeatmap();
        } else {
            if (heat) map.removeLayer(heat);
            if (markers) map.addLayer(markers);
        }
    });

    function showHeatmap() {
        const p = new URLSearchParams();
        forEachFilter(function (key, el) {
            if (el.value) p.set(key, el.value);
        });
        if (filters.lon.value && filters.lat.value) {
            p.set('coordinates', filters.lon.value + ',' + filters.lat.value);
        }

        fetch('/api/v1/planting-data/coordinates?' + p.toString())
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                if (data.error) throw new Error(data.error);
                const points = (data.coordinates || []).map(function (c) {
                    return [c.lat, c.lon, 1];
                });
                if (!points.length) {
                    showToast('No coordinate data to show.', 'warning');
                    return;
                }
                if (heat) map.removeLayer(heat);
                heat = L.heatLayer(points, {
                    radius: 20, blur: 15, maxZoom: 10,
                    gradient: {0.4: '#1976d2', 0.6: '#ff9800', 0.8: '#f44336'},
                }).addTo(map);
                map.fitBounds(points.map(function (p) {
                    return [p[0], p[1]];
                }), {padding: [20, 20], maxZoom: 10});
            })
            .catch(function (err) {
                showToast('Heatmap error: ' + err.message, 'danger');
            });
    }

    // ── Clusters toggle ─────────────────────────────────────────

    const clusterBtn = $('toggle-clusters');

    clusterBtn.addEventListener('click', function () {
        if (heatVisible) {
            heatVisible = false;
            heatmapBtn.classList.remove('active');
            heatmapBtn.innerHTML = '<i class="bi bi-fire"></i> Heatmap';
            if (heat) map.removeLayer(heat);
        }

        clusterVisible = !clusterVisible;
        clusterBtn.classList.toggle('active', clusterVisible);
        clusterBtn.innerHTML = clusterVisible
            ? '<i class="bi bi-diagram-3"></i> Points'
            : '<i class="bi bi-diagram-3"></i> Clusters';

        if (clusterVisible) {
            if (markers) map.removeLayer(markers);
            showClusters();
        } else {
            if (clusterLayer) map.removeLayer(clusterLayer);
            if (markers) map.addLayer(markers);
        }
    });

    function showClusters() {
        const bounds = map.getBounds();
        const zoom = map.getZoom();
        const p = new URLSearchParams();
        p.set('zoom', zoom);
        p.set('ne_lat', bounds.getNorth());
        p.set('ne_lng', bounds.getEast());
        p.set('sw_lat', bounds.getSouth());
        p.set('sw_lng', bounds.getWest());
        forEachFilter(function (key, el) {
            if (el.value) p.set(key, el.value);
        });

        fetch('/api/v1/planting-data/clusters?' + p.toString())
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                if (data.error) throw new Error(data.error);
                const items = data.clusters || [];
                if (!items.length) {
                    showToast('No cluster data.', 'warning');
                    return;
                }
                if (clusterLayer) map.removeLayer(clusterLayer);
                clusterLayer = L.layerGroup();

                const maxCount = items.reduce(function (m, c) {
                    return Math.max(m, c.count);
                }, 1);
                const boundsArr = [];
                items.forEach(function (c) {
                    const lat = parseFloat(c.lat);
                    const lon = parseFloat(c.lon);
                    if (isNaN(lat) || isNaN(lon)) return;
                    const r = Math.max(4, Math.min(20, 4 + (c.count / maxCount) * 16));
                    const fill = c.count > maxCount * 0.5 ? '#e53935'
                        : c.count > maxCount * 0.2 ? '#ff9800'
                            : '#1976d2';
                    const m = L.circleMarker([lat, lon], {
                        radius: r, fillColor: fill, color: '#fff',
                        weight: 1.5, fillOpacity: 0.75,
                    });
                    m.bindTooltip(c.count + ' record' + (c.count !== 1 ? 's' : ''));
                    clusterLayer.addLayer(m);
                    boundsArr.push([lat, lon]);
                });
                map.addLayer(clusterLayer);
                if (boundsArr.length) {
                    map.fitBounds(boundsArr, {padding: [20, 20], maxZoom: zoom + 1});
                }
            })
            .catch(function (err) {
                showToast('Clusters error: ' + err.message, 'danger');
            });
    }

    // ── Load filter options ─────────────────────────────────────

    function loadFilterOptions() {
        fetch('/api/v1/planting-data/filters')
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                if (data.error) return;
                const map = {
                    country: 'f-country',
                    province: 'f-province',
                    variety: 'f-variety',
                    season_type: 'f-season'
                };
                Object.keys(map).forEach(function (key) {
                    const sel = document.getElementById(map[key]);
                    if (!sel) return;

                    const vals = data[key] || [];
                    sel.innerHTML = '<option value="">All</option>';
                    vals.forEach(function (v) {
                        const opt = document.createElement('option');
                        opt.value = String(v);
                        opt.textContent = String(v);
                        sel.appendChild(opt);
                    });
                });
            })
            .catch(function () {
            });
    }

    // ── Init ────────────────────────────────────────────────────

    function escHtml(s) {
        if (s === null || s === undefined) return '';
        const d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    loadFilterOptions();
    paramsFromUrl();
    fetchData().then(r => {
        console.debug(r);
    });

})();
