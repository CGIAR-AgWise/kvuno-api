"""The OpenAPI spec documents page/per_page on every paginated endpoint, and crop_type.

get_pagination() reads them from the query string, so before PaginationQuery
nothing put them in the spec and Swagger UI offered no way to page.
"""
import pytest

PAGINATED = [
    '/api/v1/planting-data',
    '/api/v1/planting-data/filters',
    '/api/v1/planting-data/coordinates',
    '/api/v1/planting-data/clusters',
    '/api/v1/planting-data/export',
    '/api/v1/quality/conflicts',
    '/api/v1/quality/duplicates',
    '/api/v1/users/tokens',
    '/api/v1/admin/users',
]


@pytest.fixture(scope='module')
def spec():
    from app import create_app
    app = create_app()
    with app.test_client() as c:
        return c.get('/api-docs/openapi.json').get_json()


def _query_params(spec, path):
    return {p['name'] for p in spec['paths'][path]['get'].get('parameters', []) if p['in'] == 'query'}


@pytest.mark.parametrize('path', PAGINATED)
def test_paginated_endpoints_document_page_and_per_page(spec, path):
    assert {'page', 'per_page'} <= _query_params(spec, path)


@pytest.mark.parametrize('path', ['/api/v1/planting-data', '/api/v1/planting-data/coordinates',
                                  '/api/v1/quality/conflicts', '/api/v1/quality/duplicates'])
def test_crop_type_is_a_filter(spec, path):
    assert 'crop_type' in _query_params(spec, path)


def test_bad_paging_values_are_still_clamped_not_rejected():
    from app.dto.data_filters import PlantingDataFilter
    q = PlantingDataFilter(page='abc', per_page='-3')
    assert (q.page, q.per_page) == (None, -3)  # get_pagination() clamps; the model never 422s
