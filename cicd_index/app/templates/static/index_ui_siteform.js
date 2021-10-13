{
    id: "webix-instance-details",
    maxWidth: 650,
    css: "webix_dark",
    view: "form",
    type: "body",
    scroll: true,
    hidden: true,
    on: {
        'onChange': function() {
            var sitename = current_details;
            var url = "/cicd/update/site?name=" + sitename;
            var values = this.getValues();
            var values_store = {
                "_id": values._id,
                'note': values.note,
                'odoo_settings': values.odoo_settings,
                'archive': values.archive,
                'no_module_update': values.no_module_update =='1',
                'restore_no_dev_scripts': values.restore_no_dev_scripts == '1',
                'dump': values.dump,
                'docker_no_cache': values.docker_no_cache == '1',
                'do_backup_regularly': values.do_backup_regularly == '1',
                'odoo_settings_update_modules_before': values.odoo_settings_update_modules_before,
            }
            webix.ajax().post(url, values_store).then(function(res) {
            }).fail(function(response) {
                webix.message("Error: " + response.statusText, "error");
            });
        }
    },
    complexData: true,
    elements: [
        {
            view: "accordion",
            multi: true,
            type: 'space',
            rows: [
                {
                    header: "Basic Information",
                    body: {
                        rows: [
                            { cols: [
                                { view: "label", label: "Name"}, 
                                { view: 'text', name: 'name', readonly: true},
                            ]},
                            { cols: [
                                { view: "label", label: "Date registered"}, 
                                { view: 'datepicker', name: 'date_registered', readonly: true, timepicker: true, format: DATE_FORMAT},
                            ]},
                            { cols: [
                                { view: "label", label: "Updated"}, 
                                { view: 'datepicker', name: 'updated', readonly: true, timepicker: true, format: DATE_FORMAT},
                            ]},
                            { cols: [
                                { view: "label", label: "Author"}, 
                                { view: 'text', name: 'git_author', readonly: true},
                            ]},
                            { cols: [
                                { view: "label", label: "SHA"}, 
                                { view: 'text', name: 'sha', readonly: true},
                            ]},
                            { cols: [
                                { view: "label", label: "Based on"}, 
                                { view: 'text', name: 'dump_name', readonly: true},
                            ]},
                            { cols: [
                                { view: "label", label: ""}, 
                                { view: 'datepicker', name: 'dump_date', readonly: true, timepicker: true, format: DATE_FORMAT},
                            ]}
                        ]
                    }
                },
                {
                    header: "Robot Tests",
                    body: {
                        rows: [
                            { cols: [
                                { view: "label", label: "Result"}, 
                                { view: 'textarea', name: 'robot_result', readonly: true},
                            ]},
                            { cols: [
                                { view: "button", value: "Rerun Tests", name: "run_robot_tests", click: function () {
                                    run_robot_tests();
                                }}, 
                                { view: 'button', value: 'Show Detailed Results', name: "robot_results", click: function() {
                                    show_robot_results();
                                }},
                            ]}
                        ]
                    }
                },
                {
                    header: "Notes / Description",
                    body: {
                        rows: [
                            { cols: [
                                { view: 'textarea', height: 120, name: 'note', },
                            ]},
                            { cols: [
                                { view: 'textarea', name: 'git_desc', readonly: true, height: 300},
                            ]},
                        ]
                    }
                },
                {
                    header: "Build-Settings",
                    collapsed: true,
                    body: {
                        rows: [
                            { cols: [
                                { view: "label", label: "No module update"}, 
                                { view: 'checkbox', name: 'no_module_update' },
                            ]},
                            { cols: [
                                { view: "label", label: "No cache at next build"}, 
                                { view: 'checkbox', name: 'docker_no_cache' },
                            ]},
                            { cols: [
                                { view: "label", label: "Choose Dump"}, 
                                { view: "combo", name: 'dump', options: '/cicd/possible_dumps', placeholder: "Dump" },
                            ]},
                            { cols: [
                                { view: "label", label: "No Dev scripts on restore (possibly dangerous!)"}, 
                                { view: "checkbox", name: 'restore_no_dev_scripts', },
                            ]},
                            { cols: [
                                { view: "label", label: "Odoo Settings"}, 
                                { view: 'textarea', height: 150, name: 'odoo_settings', },
                            ]},
                            { cols: [
                                { view: "label", label: "Update modules always before:"}, 
                                { view: 'textarea', height: 40, name: 'odoo_settings_update_modules_before', },
                            ]},
                        ]
                    }
                },
                {
                    header: "Settings",
                    collapsed: true,
                    body: {
                        rows: [
                            { cols: [
                                { view: "label", label: "Archive"}, 
                                { view: 'checkbox', name: 'archive' },
                            ]},
                            { cols: [
                                { view: "label", label: "Backup regulary (once per day - old backups overwritten)"}, 
                                { view: 'checkbox', name: 'do_backup_regularly' },
                            ]},
                        ]
                    }
                }
            ]
        }
    ],
}