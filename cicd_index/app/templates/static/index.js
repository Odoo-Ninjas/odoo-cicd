{% include "static/tools.js" %}


var update_live_values = null;

update_live_values = function() {

    webix.ajax().get('/cicd/data/site/live_values').then(function(res) {
        var data = res.json();
        var $table = $$("table-sites")
        for (i = 0; i < data.length; i++) {
            var record = data[i];
            reload_table_item($table, record._id, record);
        }
        setTimeout(update_live_values, 5000);
    });


}

update_resources = function() {

    webix.ajax().get('/cicd/get_resources').then(function(res) {
        var $template = $$('resources-view');
        $("div#resources").html($(res.text()));
        setTimeout(update_resources, 30000);
    });

}

// start live valuen
setTimeout(update_live_values, 0);
setTimeout(update_resources, 0);

function backup_db() {
    var form_backupdb = webix.ui({
        view: "window", 
        position: 'center',
        modal: true,
        head: "Backup Database",
        width: 550,
        body: {
            view: 'form',
            complexData: true,
            elements: [
                { view: 'template', template: "Dumping postgres."},
                { view: 'text', name: 'dumpname', label: "Dumpname" },
                {
                    cols:[
                        {
                            view:"button", value:"OK", css:"webix_primary", click: function() { 
                               var values = this.getParentView().getFormView().getValues();
                                webix.ajax().get('/cicd/dump', {
                                    'name': current_details,
                                    'dumpname': values['dumpname'],
                                }).then(function(data) {
                                    form_backupdb.hide();
                                }).fail(function(data) {
                                    alert(data.statusText);
                                    console.error(data.responseText);
                                });
                            }
                        },
                        { view:"button", value:"Cancel", click: function() {
                            form_backupdb.hide();
                        }}
                    ]
                }
            ],
            on: {
                'onSubmit': function() {
                },
            }
        }
    });
    form_backupdb.show();
}


function show_logs() {
    window.open("/cicd/show_logs?name=" + current_details);
}

function shell() {
    window.open("/cicd/shell_instance?name=" + current_details);
}

function debug() {
    window.open("/cicd/debug_instance?name=" + current_details);
}

function show_mails() {
    window.open("/cicd/start?initial_path=/mailer/&name=" + current_details);
}

function start_instance() {
    window.open("/cicd/start?name=" + current_details);
}

function build_log() {
    window.open("/cicd/build_log?name=" + current_details);
}

function delete_unused() {
    webix.message('Cleaning up intermediate docker images, unused networks/containers. Unused databases.');
    webix.ajax().get("/cicd/cleanup").then(function(res) {
        webix.message("Cleanup done", "info");
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function delete_instance() {
    var form_reset = webix.ui({
        view: "window", 
        position: 'center',
        modal: true,
        head: "Delete Instance",
        width: 550,
        body: {
            view: 'form',
            complexData: true,
            elements: [
                // { view:"combo", name: 'dump', label:"Dump", options: dumps },
                { view: 'template', template: "Going to erase this instance."},
                {
                    cols:[
                        { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                form_reset.hide();
                                webix.message("Deleting in Background", "info");
                                webix.ajax().get('/cicd/delete', {
                                    'name': current_details,
                                }).then(function(data) {
                                    webix.message("Instance erased: " + current_details, "info");
                                    form_reset.hide();
                                }).fail(function(data) {
                                    alert(data.statusText);
                                    console.error(data.responseText);
                                });
                                }
                        },
                        { view:"button", value:"Cancel", click: function() {
                            form_reset.hide();
                        }}
                    ]
                }
            ],
            on: {
                'onSubmit': function() {
                },
            }
        }
    });
    form_reset.show();
}

function _build_again(do_all) {
    var url = "/cicd/build_again"
    if (do_all) {
        do_all = '1';
    } else {
        do_all = '0'
    }

    webix.ajax().get("/cicd/build_again?all=" + do_all + "&name=" + current_details).then(function(res) {
        webix.message("Triggered rebuild in Jenkins", "info");
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function build_again() {
    _build_again(false);
}

function build_again_all() {
    _build_again(true);
}

function turn_into_dev() {
    var sitename = current_details;
    var url = "/cicd/turn_into_dev?site=" + sitename;
    webix.ajax().get(url).then(function(res) {
        webix.message("Turned into dev: " + sitename, "info");
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function restart_delegator() {
    webix.message("Restarting delegator", "info");
    var url = "/cicd/restart_delegator"
    webix.ajax().get(url).then(function(res) {
        webix.message("Restarted delegator", "info");
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function start_all() {
    webix.message('Starting all docker containers; also restarting delegator after that.');
    webix.ajax().get("/cicd/start_all").then(function(res) {
        webix.message("Started all instances possible");
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function restart() {
    var site = current_details;
    webix.message('Restarting docker containers of' + current_details + '. Reporting immediately when done.');
    webix.ajax().get("/cicd/restart_docker?name=" + current_details).then(function(res) {
        webix.message("Restarted " + site);
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}
function rebuild() {
    show_reset_form(current_details);
}

function appsettings(){
    webix.ajax().get('/cicd/data/app_settings', {}).then(function(data) {
        data = data.json();
        var form = webix.ui({
            view: "window", 
            position: 'center',
            modal: true,
            head: "Settings",
            width: 550,
            body: {
                view: 'form',
                complexData: true,
                elements: [
                    { view: 'text', name: 'concurrent_builds', label: "Concurrent Builds" },
                    {
                        cols:[
                            { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                webix.ajax().post('/cicd/data/app_settings', values).then(function(data) {
                                    form.hide();
                                    values = data.json();
                                });
                                }
                            },
                            { view:"button", value:"Cancel", click: function() {
                                form.hide();
                            }}
                        ]
                    }
                ],
                on: {
                    'onSubmit': function() {
                    },
                }
            }
        });
        form.getChildViews()[1].setValues(data);
        form.show();
    });
    return false;
}

function settings(){
    webix.ajax().get('/cicd/data/sites', {'name': current_details}).then(function(data) {
        data = data.json();
        var dumps_promise = webix.ajax().get("/cicd/possible_dumps");
        dumps_promise.then(function(dumps) {
            var dumps = dumps.json();
            var form = webix.ui({
                view: "window", 
                position: 'center',
                modal: true,
                head: "Settings",
                width: 550,
                body: {
                    view: 'form',
                    complexData: true,
                    elements: [
                        { view: 'text', name: 'title', label: "Title" },
                        { view: "textarea", name: 'note', label:"Note" },
                        { view: "combo", name: 'dump', label:"Dump", options: dumps, },
                        { view: "text", name: 'backup-db', label:"Todo Dump" },
                        {
                            cols:[
                                { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                    var values = this.getParentView().getFormView().getValues();
                                    webix.ajax().post('/cicd/update/site', values).then(function(data) {
                                        form.hide();
                                        values = data.json();
                                        reload_table_item(
                                            $$("table-sites"),
                                            $$("table-sites").getSelectedItem()._id,
                                            values,
                                        )
                                    });
                                    }
                                },
                                { view:"button", value:"Cancel", click: function() {
                                    form.hide();
                                }}
                            ]
                        }
                    ],
                    on: {
                        'onSubmit': function() {
                        },
                    }
                }
            });
            form.getChildViews()[1].setValues(data[0]);
            form.show();
        });
    });
    return false;
}

var current_details = null;
function reload_details(name) {
    webix.ajax().get('/cicd/data/sites?name=' + name).then(function(data) {
        var template = $$('webix-instance-details');
        template.data = data.json()[0];
        template.refresh();
        template.show();
        $$('site-toolbar').show();
        current_details = name;
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });
}

function show_reset_form(name) {
var form_reset = webix.ui({
    view: "window", 
    position: 'center',
    modal: true,
    head: "Reset Instance",
    width: 550,
    body: {
        view: 'form',
        complexData: true,
        elements: [
            // { view:"combo", name: 'dump', label:"Dump", options: dumps },
            { view: 'template', template: "Rebuilding the instance in background. Will take some time, until instance is up again."},
            {
                cols:[
                    { view:"button", value:"OK", css:"webix_primary", click: function() { 
                            var values = this.getParentView().getFormView().getValues();
                            webix.ajax().get('/cicd/trigger/rebuild', {
                                'name': name,
                            }).then(function(data) {
                                form_reset.hide();
                            }).fail(function(response) {
                                alert(response.statusText);
                                console.error(response.responseText);
                            });
                            }
                    },
                    { view:"button", value:"Cancel", click: function() {
                        form_reset.hide();
                    }}
                ]
            }
        ],
        on: {
            'onSubmit': function() {
            },
        }
    }
});
form_reset.show();
}

var menu = {
    view: "menu",
    batch: "admin",
    autowidth: true,
    width: 120,
    type: {
        subsign: true,
    },
    data: [
        {
            id: "settings_mainmenu",
            view: "menu",
            value: "Admin...",
            config: { on: { onItemClick: clicked_menu}},
            submenu: [
                { view:"button", id:"settings", value:"Settings", click: function() {
                    settings();
                }},
                { $template:"Separator" },
                { view:"menu", id: "build_submenu", value: "Lifecycle", autowidth: true, config: { on: { onItemClick: clicked_menu}}, data: [
                    { view:"button", id:"restart", value:"Restart"},
                    { view:"button", id:"delete_instance", value:"Destroy (unrecoverable)", click: delete_instance },
                ]
                },
                { $template:"Separator" },
                { view:"button", id:"build_again", value:"Update recently changed modules" },
                { view:"button", id:"build_again_all", value:"Update all modules" },
                { view:"button", id:"rebuild", value:"Rebuild from Dump (Data lost)" },
                { $template:"Separator" },
                { view:"button", id:"backup_db", value:"Make Database Dump", click: backup_db },
                { $template:"Separator" },
                { view:"button", id:"turn_into_dev", value: 'Apply Developer Settings (Password, Cronjobs)', click: turn_into_dev}
            ]
        },
    ],
}
webix.ajax().get('/cicd/start_info').then(function(startinfo) {
    startinfo = startinfo.json();

    webix.ui(
            {
                view: "sidemenu",
                id: "sidemenu1",
                css: "webix_dark",
                body:{
                    view:"list",
                    borderless:true,
                    scroll: false,
                    template: "<span style='margin-right: 10px;' class='webix_icon fas fa-#icon#'></span> #value#",
                    on:{
                        onItemClick: clicked_menu,
                    },
                    data:[
                        { view:"button", id:"restart_delegator", icon: 'recycle', value:"Restart Delegator", batch: 'admin'},
                        { view:"button", id:"start_all", icon: 'play', value:"Start All Docker Containers", batch: 'admin', click: clicked_menu,},
                        { view:"button", id:"delete_unused", icon: 'eraser', value:"Spring Clean", batch: 'admin'},
                        { view:"button", id:"users_admin", value:"Users", icon: "users", batch: 'admin' },
                        { view:"button", id:"appsettings", value:"App Settings", icon: "cog", batch: 'admin' },
                        { view:"button", id:"logout", value:"Logout", icon: "sign-out-alt", batch: 'user'},
                    ]
                }
            },
    );

    webix.ui({
        type: 'wide',
        cols: [
            {
                rows: [
                    {view: "toolbar", id:"toolbar_header", elements:[
                        {

                            view: "icon", icon: "fas fa-bars",
                            click: function(){
                                if( $$("sidemenu1").config.hidden){
                                    $$("sidemenu1").show(false, false);
                                }
                                else
                                    $$("sidemenu1").hide();
                            }
                        },
                        {
                            view: "template",
                            type: "header",
                            css: "webix_dark",
                            template: "CICD Feature Branches"
                        },
                    ]},
                    {
                        id: "resources-view",
                        view: "template",
                        type: "header",
                        template: "<div id='resources'></div>",
                    },
                    {
                        view:"text", 
                        placeholder:"Filter grid",
                        on:{
                            onTimedKeyPress:function(){
                            var text = this.getValue().toLowerCase();
                            var table = $$("table-sites");
                            var columns = table.config.columns;
                            table.filter(function(obj){
                                for (var i=0; i<columns.length; i++) {
                                    if (obj[columns[i].id].toString().toLowerCase().indexOf(text) !== -1) return true;
                                    return false;
                                }
                            })
                            }
                        }
                    },
                    {
                        id: 'table-sites',
                        view: "datatable",
                        navigation: true,
                        headerRowHeight: 60,
                        rowHeight: 40,
                        select: 'row',
                        autoConfig: false,
                        url: '/cicd/data/sites',
                        editable: false,
                        data: [],
                        leftSplit: 0,
                        scrollX: false,
                        on: {
                            onSelectChange:function(){
                                if (!this.getSelectedItem()) {
                                    return;
                                }
                                reload_details(this.getSelectedItem().name);
                            },
                            onItemClick: function(id, e, trg) {
                                //if (id.column === 'start_instance') {
                                //    var name = this.getSelectedItem().name;
                                //    start_instance(name);
                                //}
                            }
                        },
                        columns:[
                            { id: 'name', header: 'Name', minWidth: 150},
                            { id: 'title', header: 'Title', minWidth: 180},
                            { id: 'success', header: 'Success', template: "{common.checkbox()}", disable: true, minWidth: 80},
                            { id: 'is_building', header: 'Building', template: "{common.checkbox()}", disable: true},
                            { id: 'docker_state', header: 'Docker', },
                            //{ id: 'updated', header: 'Updated', minWidth: 150,},
                            { id: 'duration', header: 'Duration [s]'},
                        ],
                    },
                ]
            },
            {
            rows: [
                {
                    view: 'toolbar',
                    css: "webix_dark",
                    id: 'site-toolbar',
                    hidden: true,
                    elements: [
                        menu,
                        { view:"button", id:"build_log", value:"Build Log", width:150, align:"left", click: build_log, batch: 'admin' },
                        { view:"button", id:"start", value:"Open UI", width:100, align:"right", click: start_instance, batch: 'user' },
                        { view:"button", id:"start_mails", value:"Mails", width:100, align:"right", click: show_mails, batch: 'user' },
                        { view:"button", id:"start_logging", value:"Live Log", width:100, align:"right", click: show_logs, batch: 'admin' },
                        { view:"button", id:"start_shell", value:"Shell", width:100, align:"right", click: shell, batch: 'admin' },
                        { view:"button", id:"start_debugging", value:"Debug", width:100, align:"right", click: debug, batch: 'admin' },
                    ],
                },
                {
                    id: "webix-instance-details",
                    maxWidth: 650,
                    css: "webix_dark",
                    view: "template",
                    type: "body",
                    template: "html->instance-template",
                    hidden: true,
                },
            ]
            }
        ]
    });

    webix.ui.fullScreen();

    if (!startinfo.is_admin) {
        $$("site-toolbar").showBatch('admin', false);
        $$("site-toolbar-common").showBatch('admin', false);
        $$("sidemenu1").showBatch('admin', false);
    }

});