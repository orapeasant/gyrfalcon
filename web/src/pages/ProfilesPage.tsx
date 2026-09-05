import React, { useState, useEffect } from 'react';
import { fetchJSON } from '../lib/api';

interface ProviderProfile {
  id: string;
  name: string;
  provider_type: string;
  base_url: string;
  is_active: boolean;
  models: string[];
}

export function ProfilesPage() {
  const [profiles, setProfiles] = useState<ProviderProfile[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadProfiles(); }, []);

  async function loadProfiles() {
    setLoading(true);
    try {
      const data = await fetchJSON('/api/profiles');
      setProfiles(data.profiles || []);
    } catch { setProfiles([]); }
    setLoading(false);
  }

  async function activateProfile(id: string) {
    await fetchJSON(`/api/profiles/${id}/activate`, { method: 'POST' });
    loadProfiles();
  }

  if (loading) return <div className="p-4">Loading profiles...</div>;

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">Provider Profiles</h1>

      <div className="space-y-3">
        {profiles.length === 0 && <p className="text-gray-500">No provider profiles configured.</p>}
        {profiles.map(profile => (
          <div key={profile.id} className={`border rounded p-4 ${profile.is_active ? 'border-green-400 bg-green-50' : ''}`}>
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold">
                  {profile.name}
                  {profile.is_active && <span className="ml-2 text-xs bg-green-200 text-green-700 rounded px-1">Active</span>}
                </h3>
                <p className="text-sm text-gray-600">{profile.provider_type} — {profile.base_url || 'default'}</p>
                {profile.models.length > 0 && (
                  <div className="flex gap-1 mt-1 flex-wrap">
                    {profile.models.map(m => (
                      <span key={m} className="text-xs bg-gray-200 rounded px-1">{m}</span>
                    ))}
                  </div>
                )}
              </div>
              {!profile.is_active && (
                <button className="text-sm bg-green-100 text-green-700 px-3 py-1 rounded" onClick={() => activateProfile(profile.id)}>
                  Activate
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
