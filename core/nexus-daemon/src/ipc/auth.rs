//! Autenticação local do IPC por credenciais de peer (`SO_PEERCRED`).
//!
//! O daemon aceita apenas conexões do mesmo UID efetivo do processo. A
//! verificação é determinística e fail-closed: falha ao ler as credenciais ou
//! UID divergente encerram a conexão antes de qualquer frame de controle.

use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::Path;

/// Credenciais do processo do outro lado do soquete Unix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCredentials {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

#[derive(Debug, thiserror::Error)]
pub enum PeerAuthError {
    #[error("falha ao ler credenciais do peer: {0}")]
    Credentials(String),
    #[error("UID do peer não autorizado")]
    UnauthorizedUid,
    #[error("falha ao ajustar permissões do socket: {0}")]
    SocketPermissions(String),
}

/// Lê as credenciais do peer via `SO_PEERCRED` (Linux).
///
/// `std::os::unix::net::UnixStream::peer_cred` ainda é instável neste
/// toolchain; a chamada direta a `getsockopt` usa a mesma primitive do kernel.
pub fn peer_credentials(stream: &UnixStream) -> Result<PeerCredentials, PeerAuthError> {
    let mut cred = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: o ponteiro aponta para `ucred` inicializado e `length` descreve
    // seu tamanho; o fd pertence ao `UnixStream` vivo durante a chamada.
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut libc::ucred as *mut libc::c_void,
            &mut length,
        )
    };
    if result != 0 {
        return Err(PeerAuthError::Credentials(
            std::io::Error::last_os_error().to_string(),
        ));
    }
    Ok(PeerCredentials {
        uid: cred.uid,
        gid: cred.gid,
        pid: cred.pid,
    })
}

/// UID efetivo do processo do daemon.
pub fn current_uid() -> u32 {
    // SAFETY: `geteuid` não recebe ponteiros e não altera estado.
    unsafe { libc::geteuid() }
}

/// Autoriza apenas o mesmo UID efetivo do daemon.
///
/// Não há exceção para root: um daemon rodando como usuário comum não deve
/// aceitar comandos de controle de outro UID, mesmo privilegiado.
pub fn authorize_peer(peer_uid: u32, expected_uid: u32) -> Result<(), PeerAuthError> {
    if peer_uid == expected_uid {
        Ok(())
    } else {
        Err(PeerAuthError::UnauthorizedUid)
    }
}

/// Restringe o soquete ao dono (`0600`).
pub fn restrict_socket_permissions(path: &Path) -> Result<(), PeerAuthError> {
    let permissions = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, permissions)
        .map_err(|error| PeerAuthError::SocketPermissions(error.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_uid_is_authorized() {
        assert!(authorize_peer(1000, 1000).is_ok());
    }

    #[test]
    fn different_uid_is_rejected_including_root() {
        assert!(matches!(
            authorize_peer(0, 1000),
            Err(PeerAuthError::UnauthorizedUid)
        ));
        assert!(matches!(
            authorize_peer(1001, 1000),
            Err(PeerAuthError::UnauthorizedUid)
        ));
    }

    #[test]
    fn peer_cred_of_socketpair_matches_current_uid() {
        let (left, _right) = UnixStream::pair().expect("socketpair");
        let peer = peer_credentials(&left).expect("SO_PEERCRED");
        assert_eq!(peer.uid, current_uid());
        assert_eq!(peer.pid, std::process::id() as i32);
    }

    #[test]
    fn socket_permissions_are_owner_only() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("restricted.sock");
        std::fs::File::create(&path).unwrap();
        restrict_socket_permissions(&path).unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }
}
