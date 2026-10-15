"""Isolated tests for MAB profile persistence and safety boundaries."""
import json, stat
import pytest
from veya.mab import MABManager, clean_site, safe_url, redact_visible_text

@pytest.fixture
def manager(tmp_path):
    item=MABManager(); item.base_dir=tmp_path/'mab'; item.profiles_dir=item.base_dir/'profiles'
    return item

def create(manager,name='Work',site='https://example.com'):
    return manager.create_profile(name,site,'unit-test')

def test_profile_directory_modes(manager):
    p=create(manager); folder=manager._profile_dir(p.id)
    assert stat.S_IMODE(manager.base_dir.stat().st_mode)==0o700
    assert stat.S_IMODE(folder.stat().st_mode)==0o700
    assert stat.S_IMODE((folder/'browser-data').stat().st_mode)==0o700

def test_metadata_has_only_public_fields(manager):
    p=create(manager); data=json.loads((manager._profile_dir(p.id)/'metadata.json').read_text())
    assert set(data)=={'id','name','site','account_label','status','created_at','last_used'}
    assert stat.S_IMODE((manager._profile_dir(p.id)/'metadata.json').stat().st_mode)==0o600

def test_profile_crud_and_rename(manager):
    p=create(manager); changed=manager.update_profile(p.id,name='Renamed')
    assert changed.name=='Renamed' and manager.get_profile('renamed').id==p.id
    assert manager.list_profiles()[0].name=='Renamed'

def test_duplicate_profile_rejected(manager):
    create(manager)
    with pytest.raises(ValueError): create(manager,'work','https://example.com/')

def test_delete_needs_confirmation(manager):
    p=create(manager)
    with pytest.raises(ValueError): manager.delete_profile(p.id)
    assert manager.get_profile(p.id) is not None

def test_confirmed_delete_removes_only_selected_profile(manager):
    p=create(manager); other=create(manager,'Other','https://example.org')
    assert manager.delete_profile(p.id,confirm=True)
    assert manager.get_profile(p.id) is None and manager.get_profile(other.id) is not None

@pytest.mark.parametrize('site,expected',[
    ('https://example.com?next=private','https://example.com/'),
    ('http://example.org/path#top','http://example.org/path'),
    ('https://example.com/','https://example.com/'),
    ('https://example.net/login?code=x','https://example.net/login'),
])
def test_site_metadata_is_normalized(site,expected):
    assert clean_site(site)==expected

@pytest.mark.parametrize('site',['file:///tmp/a','javascript:alert(1)','https://user:pass@example.com','', 'ftp://example.com'])
def test_unsafe_site_is_rejected(site):
    with pytest.raises(ValueError): clean_site(site)

@pytest.mark.parametrize('url,expected',[
    ('https://example.com/a?access_token=secret','https://example.com/a'),
    ('http://example.org/','http://example.org/'),
    ('javascript:alert(1)','about:blank'),
    ('about:blank','about:blank'),
])
def test_url_output_removes_query_or_unsupported_scheme(url,expected):
    assert safe_url(url)==expected

@pytest.mark.parametrize('value',[
    'password=raw-value','cookie=raw-value','access_token=raw-value',
    'authorization=raw-value','refresh_token=raw-value',
])
def test_visible_text_redacts_sensitive_pairs(value):
    assert 'raw-value' not in redact_visible_text(value)

def test_disable_and_reenable_profile(manager):
    p=create(manager)
    assert manager.update_profile(p.id,status='disabled').status=='disabled'
    assert manager.update_profile(p.id,status='active').status=='active'

def test_path_traversal_is_rejected(manager):
    with pytest.raises(ValueError): manager._profile_dir('../outside')
