-- Undo mesh.sql. Leaves the added columns (harmless, keeps data); drops the push path.
DROP TRIGGER IF EXISTS a2a_mesh_notify ON a2a_messages;
DROP TRIGGER IF EXISTS a2a_mesh_before ON a2a_messages;
DROP FUNCTION IF EXISTS a2a_mesh_notify();
DROP FUNCTION IF EXISTS a2a_mesh_before();
DROP TABLE IF EXISTS agent_presence;
